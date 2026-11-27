# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway-owned MCP client for research services, bound to outside review.

The gateway connects to research services with OpenHands' public ``MCPClient`` over
Streamable HTTP. The agent receives no MCP configuration. A worker sees each enabled
tool only as a fixed client tool; a call becomes an outside operation that the
researcher reviews before the gateway sends it, and its result reaches the model only
through a separately reviewed release.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionError,
    ExternalActionObservation,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    ExternalInputReference,
    ExternalOperationEstimate,
    ExternalOperationReview,
    canonical_request,
    external_action_records,
)
from heartwood.core_adapter.external_results import external_result_records
from heartwood.gateway._access import (
    AccessUnavailableError,
    ServiceTokens,
    TokenExchangeRefusedError,
)
from heartwood.gateway._credentials import CredentialStore
from heartwood.gateway._mcp_policy import (
    GATEWAY_FIELDS,
    TARGET_FIELD,
    McpDestination,
    McpServerError,
    McpServerSetting,
    McpToolSelection,
    binding_supported,
    check_resolved_host,
    definition_digest,
    destination_for,
    mcp_credential_binding,
    server_url,
    token_authentication,
    tool_definition,
    url_origin,
    worker_parameters,
    worker_tool_name,
)
from heartwood.gateway._platform_operations import (
    call_structured,
    link_operations,
    operations_report,
    request_proposal,
)
from heartwood.gateway._platform_planning import (
    AGENT_RESULT_BYTES,
    PLANNING_SERVICE_ID,
    PlanningStateError,
)
from heartwood.schemas import JsonValue, PlatformOperationObservation, PlatformOperationProposal
from heartwood.session import EventKind

if TYPE_CHECKING:
    from openhands.sdk.mcp import MCPClient
    from openhands.sdk.tool.client_tool import ClientToolSpec

    from heartwood.core_adapter import SessionService
    from heartwood.gateway._platform_planning import PlatformPlanning
    from heartwood.gateway._project_config import ProjectConfig, ProjectConfigStore

type McpToolStatus = Literal["ready", "changed", "missing", "disallowed", "unsupported"]

# Reconciliation queries the service; snapshot reads should not repeat it on every request.
_RECONCILE_INTERVAL_SECONDS = 10.0
_DISCOVERY_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class McpBoundTool:
    """One tool enabled for a session, with the server and destination it was bound under."""

    server: McpServerSetting
    tool: McpToolSelection
    destination: McpDestination

    @property
    def worker_name(self) -> str:
        """Return the client-tool name the worker calls."""
        return worker_tool_name(self.server.server_id, self.tool)

    @property
    def identity(self) -> dict[str, object]:
        """Return everything a researcher reviewed or the deployment allowed for this tool."""
        return {
            "server_id": self.server.server_id,
            "url": self.server.url,
            "tool": self.tool.name,
            "sha256": self.tool.sha256,
            "destination": self.destination.model_dump(mode="json"),
        }

    def client_spec(self) -> ClientToolSpec:
        """Describe the tool to the worker without executor, credential or gateway fields."""
        from openhands.sdk.tool.client_tool import ClientToolSpec

        definition = self.tool.parsed
        description = str(definition.get("description") or self.tool.name)
        return ClientToolSpec(
            name=self.worker_name,
            description=(
                f"{description}\n\nThis calls {self.tool.name} on the research service "
                f"{self.server.label}. The researcher reviews the call before it is sent, "
                "and the result arrives later as a separate message."
            ),
            parameters=worker_parameters(definition),
        )


class McpServerManager:
    """Own research MCP server settings, credentials, discovery and session binding."""

    def __init__(
        self,
        *,
        config_store: ProjectConfigStore,
        credential_store: CredentialStore,
        destinations: Sequence[McpDestination],
        worker_available: bool,
        project_id: str | None = None,
        service_tokens: ServiceTokens | None = None,
    ) -> None:
        self._config_store = config_store
        # The project the gateway names to services: the platform project of the launch
        # context, or this project's identifier.
        self.project_id = project_id
        self._credentials = credential_store
        self._service_tokens = service_tokens
        self._destinations = tuple(destinations)
        self._worker_available = worker_available
        # Definitions from the latest test in this process, by server and tool name.
        self._discovered: dict[str, dict[str, str]] = {}
        self._lock = Lock()
        # Serializes token saves, and the check that their server is still configured,
        # with disconnect's check for other servers of the same service.
        self._token_lock = Lock()

    def _servers(self) -> tuple[McpServerSetting, ...]:
        return self._config_store.load().mcp_servers

    def _server(self, server_id: str) -> McpServerSetting:
        server = next((item for item in self._servers() if item.server_id == server_id), None)
        if server is None:
            raise McpServerError("mcp-unavailable", f"unknown research service: {server_id}")
        return server

    def _destination(self, server: McpServerSetting) -> McpDestination | None:
        try:
            return destination_for(server.url, self._destinations)
        except McpServerError:
            return None

    def _change(
        self,
        server_id: str,
        transform: Callable[[McpServerSetting | None], McpServerSetting | None],
    ) -> None:
        """Apply one change to the stored server under the configuration lock, in place."""

        def apply(current: ProjectConfig) -> ProjectConfig:
            servers = list(current.mcp_servers)
            index = next((i for i, item in enumerate(servers) if item.server_id == server_id), None)
            changed = transform(None if index is None else servers[index])
            if index is None and changed is not None:
                servers.append(changed)
            elif index is not None and changed is None:
                del servers[index]
            elif index is not None and changed is not None:
                servers[index] = changed
            return replace(current, mcp_servers=tuple(servers))

        self._config_store.update(apply)

    def token(self, destination: McpDestination) -> str | None:
        """Resolve a destination's bearer token in the gateway; never in project files.

        A destination that declares a token exchange gets the token exchanged for the
        researcher's sign-in and never a stored one; None means no sign-in is live.
        """
        exchange = destination.token_exchange
        if exchange is None:
            return self._credentials.resolve(mcp_credential_binding(destination.service_id))
        if self._service_tokens is None:
            return None
        try:
            return self._service_tokens(exchange.scope, exchange.audience)
        except AccessUnavailableError:
            raise McpServerError(
                "gateway-identity-provider",
                "the identity provider is unavailable, so the research service has no token",
            ) from None
        except TokenExchangeRefusedError:
            raise McpServerError(
                "gateway-identity",
                "the identity provider issued no token for this research service",
            ) from None

    @staticmethod
    def _refuse_entered(destination: McpDestination, token: str | None) -> None:
        if destination.token_exchange is not None and _entered(token) is not None:
            raise McpServerError(
                "mcp-credential",
                "this research service takes its token from the researcher's sign-in "
                "and accepts no entered token",
            )

    def _save_token(self, destination: McpDestination, token: str | None, remember: bool) -> None:
        self._refuse_entered(destination, token)
        if token is not None and token.strip():
            self._credentials.save(
                mcp_credential_binding(destination.service_id), token.strip(), remember=remember
            )

    def _keep_token(
        self,
        server: McpServerSetting,
        destination: McpDestination,
        token: str | None,
        remember: bool,
    ) -> None:
        """Keep an accepted token while the server it was entered for is still configured."""
        with self._token_lock:
            current = next(
                (item for item in self._servers() if item.server_id == server.server_id), None
            )
            if current is None or current.url != server.url:
                raise McpServerError(
                    "mcp-unavailable",
                    f"{server.server_id} changed meanwhile; the token was not kept",
                )
            self._save_token(destination, token, remember)

    def _list_tools(
        self, server: McpServerSetting, destination: McpDestination, token: str | None
    ) -> tuple[dict[str, str], Any]:
        with self.connect(server, destination, token=token) as client:
            try:
                tools = cast(
                    list[Any],
                    client.call_async_from_sync(
                        client.list_tools, timeout=_DISCOVERY_TIMEOUT_SECONDS
                    ),
                )
            except Exception:
                raise McpServerError(
                    "mcp-unavailable", "the research service did not list its tools"
                ) from None
            initialized = client.initialize_result
        definitions = {str(tool.name): tool_definition(tool) for tool in tools}
        with self._lock:
            self._discovered[server.server_id] = definitions
        return definitions, None if initialized is None else initialized.capabilities

    # Shared operations

    def add(
        self, server_id: str, label: str, url: str, token: str | None, remember: bool
    ) -> dict[str, object]:
        """Record a server at an approved destination; nothing is contacted or enabled."""
        server = McpServerSetting(server_id=server_id, label=label.strip(), url=server_url(url))
        try:
            server.validate()
        except ValueError as error:
            raise McpServerError("mcp-destination", str(error)) from None
        destination = destination_for(server.url, self._destinations)
        self._refuse_entered(destination, token)

        def insert(current: McpServerSetting | None) -> McpServerSetting:
            if current is not None:
                raise McpServerError("mcp-unavailable", f"{server_id} is already configured")
            return server

        with self._token_lock:
            self._change(server_id, insert)
            self._save_token(destination, token, remember)
        return self.settings()

    def test(self, server_id: str, token: str | None, remember: bool) -> dict[str, object]:
        """Connect, list tools and report each definition; discovery authorizes nothing.

        A token supplied here is kept only once the server accepted it.
        """
        server = self._server(server_id)
        destination = destination_for(server.url, self._destinations)
        definitions, capabilities = self._list_tools(server, destination, _entered(token))
        self._keep_token(server, destination, token, remember)
        operations: dict[str, object] | None = None
        if destination.operations is not None:
            try:
                with self.connect(server, destination, token=_entered(token)) as client:
                    operations = operations_report(
                        client, destination, self.project_id, definitions
                    )
            except McpServerError:
                operations = operations_report(None, destination, None, definitions)
        selected = {tool.name: tool.sha256 for tool in server.tools}
        discovered = []
        for name, definition in sorted(definitions.items()):
            parsed = json.loads(definition)
            digest = definition_digest(definition)
            discovered.append(
                {
                    "name": name,
                    "description": str(parsed.get("description") or ""),
                    "sha256": digest,
                    "parameters": _parameters(parsed),
                    "definition": definition,
                    "annotations": _annotations(parsed),
                    "allowed": name in destination.tools,
                    "accepts_operation_binding": binding_supported(
                        parsed, cancel=destination.cancels(name)
                    ),
                    "selected": name in selected,
                    "changed": name in selected and selected[name] != digest,
                }
            )
        return {
            "server": self._summary(server),
            "tools": discovered,
            "resources": {
                "advertised": bool(capabilities is not None and capabilities.resources),
                "support": "unsupported",
            },
            "prompts": {
                "advertised": bool(capabilities is not None and capabilities.prompts),
                "support": "unsupported",
            },
            "operations": operations,
        }

    def select_tools(
        self,
        server_id: str,
        reviewed: Sequence[tuple[str, str]],
        *,
        token: str | None = None,
        remember: bool = False,
    ) -> dict[str, object]:
        """Record the researcher's review of exact definitions, listed again now.

        Each choice names a tool and the digest of the definition the researcher reviewed;
        a definition that differs from the server's current one is refused.
        """
        server = self._server(server_id)
        destination = destination_for(server.url, self._destinations)
        names = [name for name, _ in reviewed]
        if len(set(names)) != len(names):
            raise McpServerError("mcp-binding", "each tool can be selected once")
        for name in names:
            if name not in destination.tools:
                raise McpServerError("mcp-binding", f"the deployment does not allow {name}")
        definitions = self._list_tools(server, destination, _entered(token))[0] if reviewed else {}
        selections: list[McpToolSelection] = []
        for name, digest in reviewed:
            definition = definitions.get(name)
            if definition is None or definition_digest(definition) != digest:
                raise McpServerError(
                    "mcp-tool-changed", f"{name} differs from the reviewed definition"
                )
            if not binding_supported(json.loads(definition), cancel=destination.cancels(name)):
                raise McpServerError(
                    "mcp-binding",
                    f"{name} does not accept a project and operation identifier"
                    + (" and the operation it cancels" if destination.cancels(name) else ""),
                )
            selections.append(McpToolSelection(name=name, definition=definition))

        def select(current: McpServerSetting | None) -> McpServerSetting:
            if current is None or current.url != server.url:
                raise McpServerError("mcp-unavailable", f"{server_id} changed meanwhile")
            updated = replace(current, tools=tuple(selections))
            try:
                updated.validate()
            except ValueError as error:
                raise McpServerError("mcp-binding", str(error)) from None
            return updated

        self._change(server_id, select)
        # A token is kept only when it listed the tools; a refused selection keeps the old one.
        if reviewed:
            self._keep_token(server, destination, token, remember)
        return self.settings()

    def set_enabled(self, server_id: str, enabled: bool) -> dict[str, object]:
        """Enable or disable a server for sessions that start afterwards."""

        def toggle(current: McpServerSetting | None) -> McpServerSetting:
            if current is None:
                raise McpServerError("mcp-unavailable", f"unknown research service: {server_id}")
            if enabled:
                destination_for(current.url, self._destinations)
            return replace(current, enabled=enabled)

        self._change(server_id, toggle)
        return self.settings()

    def disconnect(self, server_id: str) -> dict[str, object]:
        """Remove a server, and its token unless another server uses the same service.

        Completed remote work is not undone.
        """
        server = self._server(server_id)
        destination = self._destination(server)
        with self._token_lock:
            # The token goes first, so a failure to forget it leaves the server listed.
            if destination is not None and not any(
                item.server_id != server_id and self._destination(item) == destination
                for item in self._servers()
            ):
                self._credentials.forget(mcp_credential_binding(destination.service_id))
            self._change(server_id, lambda _current: None)
        with self._lock:
            self._discovered.pop(server_id, None)
        return self.settings()

    def settings(self) -> dict[str, object]:
        """Return every server, its tools and the tool set enabled for new sessions."""
        reason = self.unavailable_reason()
        return {
            "availability": "unavailable" if reason else "available",
            "unavailable_reason": None if reason is None else reason.detail,
            "unavailable_code": None if reason is None else reason.code,
            "destinations": [
                {
                    "service_id": item.service_id,
                    "origin": item.origin,
                    "tools": list(item.tools),
                    "max_result_bytes": item.max_result_bytes,
                    "credential_binding": mcp_credential_binding(item.service_id),
                    "private_address": item.private_address,
                }
                for item in self._destinations
            ],
            "credential_store": self._credentials.availability().safe_dict(),
            "servers": [self._summary(server) for server in self._servers()],
        }

    def unavailable_reason(self) -> McpServerError | None:
        """Return why no session can call research services, if none can."""
        if not self._worker_available:
            return McpServerError(
                "mcp-worker-required", "research services run only in isolated worker sessions"
            )
        if not self._destinations:
            return McpServerError(
                "mcp-destination", "the deployment allows no research service destinations"
            )
        return None

    def _summary(self, server: McpServerSetting) -> dict[str, object]:
        destination = self._destination(server)
        with self._lock:
            discovered = self._discovered.get(server.server_id)
        tools = [
            {
                "name": tool.name,
                "worker_tool": worker_tool_name(server.server_id, tool),
                "description": str(tool.parsed.get("description") or ""),
                "sha256": tool.sha256,
                "parameters": _parameters(tool.parsed),
                "definition": tool.definition,
                "annotations": _annotations(tool.parsed),
                "status": _tool_status(tool, destination, discovered),
            }
            for tool in server.tools
        ]
        enabled = (
            [str(item["worker_tool"]) for item in tools if item["status"] == "ready"]
            if server.enabled and destination is not None and self.unavailable_reason() is None
            else []
        )
        return {
            "server_id": server.server_id,
            "label": server.label,
            "url": server.url,
            "origin": url_origin(server.url),
            "service_id": None if destination is None else destination.service_id,
            "destination": "allowed" if destination is not None else "refused",
            "enabled": server.enabled,
            "authentication": token_authentication(
                None if destination is None else self._safe_token(destination)
            ).safe_dict(),
            "tools": tools,
            "enabled_tools": enabled,
        }

    def _safe_token(self, destination: McpDestination) -> str | None:
        try:
            return self.token(destination)
        except ValueError:
            return None

    # Session binding

    def bound_tools(self) -> tuple[McpBoundTool, ...]:
        """Return the tools a session starting now may offer its worker."""
        if self.unavailable_reason() is not None:
            return ()
        bound: list[McpBoundTool] = []
        for server in self._servers():
            destination = self._destination(server)
            if not server.enabled or destination is None:
                continue
            with self._lock:
                discovered = self._discovered.get(server.server_id)
            bound.extend(
                McpBoundTool(server, tool, destination)
                for tool in server.tools
                if _tool_status(tool, destination, discovered) == "ready"
            )
        return tuple(bound)

    def live_identity(self, bound: McpBoundTool) -> dict[str, object] | None:
        """Return a bound tool's identity while its current settings still allow it."""
        if self.unavailable_reason() is not None:
            return None
        server = next(
            (item for item in self._servers() if item.server_id == bound.server.server_id), None
        )
        if server is None or not server.enabled or server.url != bound.server.url:
            return None
        destination = self._destination(server)
        tool = next((item for item in server.tools if item.name == bound.tool.name), None)
        if destination != bound.destination or tool != bound.tool:
            return None
        return bound.identity

    def require_current(self, bound: McpBoundTool) -> None:
        """Refuse a call whose server, selection, destination or credential no longer holds."""
        if self.live_identity(bound) is None:
            raise ExternalActionError("the research service tool is no longer enabled")
        try:
            token = self.token(bound.destination)
        except McpServerError as error:
            # Identity refusals keep their stable code, so the researcher sees HW-INGRESS-004.
            raise ExternalActionError(
                str(error) if error.code.startswith("HW-INGRESS-") else error.detail
            ) from None
        if bound.destination.token_exchange is not None and token is None:
            raise ExternalActionError(
                str(
                    McpServerError(
                        "gateway-identity",
                        "the research service waits until the researcher signs in at the gateway",
                    )
                )
            )
        if token_authentication(token).status == "expired":
            raise ExternalActionError("the research service credential is expired")

    @contextmanager
    def connect(
        self,
        server: McpServerSetting,
        destination: McpDestination,
        *,
        token: str | None = None,
    ) -> Iterator[MCPClient]:
        """Open a Streamable HTTP session; the client follows redirects only within the origin.

        The address check runs before the connection, and the client resolves the name
        again; the platform's egress controls bound the connection itself.
        """
        from fastmcp.client.auth.bearer import BearerAuth
        from fastmcp.client.transports import StreamableHttpTransport
        from openhands.sdk.mcp import MCPClient

        if destination.token_exchange is not None:
            self._refuse_entered(destination, token)
            token = self.token(destination)
            if token is None:
                raise McpServerError(
                    "gateway-identity",
                    "the research service waits until the researcher signs in at the gateway",
                )
        token = self.token(destination) if token is None else token
        if token_authentication(token).status == "expired":
            raise McpServerError("mcp-credential", "the research service token is expired")
        parsed = urlsplit(server.url)
        check_resolved_host(
            parsed.hostname or "",
            parsed.port or (443 if parsed.scheme == "https" else 80),
            private_address=destination.private_address,
        )
        verify: ssl.SSLContext | None = None
        if destination.ca_file is not None:
            try:
                verify = ssl.create_default_context(cafile=str(destination.ca_file))
            except (OSError, ValueError):
                raise McpServerError(
                    "mcp-unavailable", "the research service's certificate authority is unusable"
                ) from None
        client = MCPClient(  # type: ignore[no-untyped-call]
            StreamableHttpTransport(
                server.url,
                auth=BearerAuth(token) if token else None,
                max_sse_event_size=destination.max_result_bytes + 16 * 1024,
                verify=verify,
            ),
            timeout=destination.call_timeout_seconds,
        )
        try:
            try:
                client.call_async_from_sync(
                    client.connect, timeout=destination.call_timeout_seconds
                )
            except Exception:
                raise McpServerError(
                    "mcp-unavailable", "the research service refused or did not answer"
                ) from None
            yield client
        finally:
            client.sync_close()


def _entered(token: str | None) -> str | None:
    return token.strip() if token is not None and token.strip() else None


def _parameters(definition: dict[str, Any]) -> str:
    """Return the input schema the worker sees, as canonical JSON for review."""
    return json.dumps(worker_parameters(definition), sort_keys=True, separators=(",", ":"))


def _annotations(definition: dict[str, Any]) -> dict[str, bool | None]:
    value = definition.get("annotations")
    annotations = value if isinstance(value, dict) else {}

    def hint(name: str) -> bool | None:
        item = annotations.get(name)
        return item if isinstance(item, bool) else None

    return {
        "read_only": hint("readOnlyHint"),
        "destructive": hint("destructiveHint"),
        "idempotent": hint("idempotentHint"),
        "open_world": hint("openWorldHint"),
    }


def _tool_status(
    tool: McpToolSelection,
    destination: McpDestination | None,
    discovered: dict[str, str] | None,
) -> McpToolStatus:
    if destination is None or tool.name not in destination.tools:
        return "disallowed"
    if not binding_supported(tool.parsed, cancel=destination.cancels(tool.name)):
        return "unsupported"
    if discovered is not None:
        if tool.name not in discovered:
            return "missing"
        if definition_digest(discovered[tool.name]) != tool.sha256:
            return "changed"
    return "ready"


class McpOutsideAdapter:
    """Trusted outside-service adapter for the MCP tools bound to one session.

    Each call reconnects, verifies the reviewed tool definition, and sends the reviewed
    arguments together with the gateway's project and operation identity. The service
    enforces the binding: it scopes each call to the project its credential allows, and
    a repeated operation identifier never causes a second effect. The deployment names
    the read-only tool that reports an operation by identifier.

    With a launch context, calls name its platform project and observations must carry
    its project context. A destination with an operation profile also describes each
    submission before review, and a cancellation names an earlier operation of the
    session, which the gateway sends from its own record.

    With planning, the session also offers the gateway's planning tools. Their calls
    pass the same review and result release, and run in the gateway's planning state,
    so every interface shows what the agent recorded.
    """

    def __init__(
        self,
        manager: McpServerManager,
        bound: Sequence[McpBoundTool],
        *,
        planning: PlatformPlanning | None = None,
    ) -> None:
        self._manager = manager
        self._bound = {item.worker_name: item for item in bound}
        self._planning = planning
        self._planning_tools = (
            {} if planning is None else {item.name: item for item in planning.client_tools}
        )
        self.last_reconciled = 0.0
        # The last session event whose platform operation records were linked to planning.
        self.linked_sequence = 0

    @property
    def planning(self) -> PlatformPlanning | None:
        """Return the planning state that reviewed operations are linked to, if any."""
        return self._planning

    @property
    def client_tools(self) -> tuple[ClientToolSpec, ...]:
        """Return the fixed client tools the session's worker receives."""
        return (
            *(item.client_spec() for item in self._bound.values()),
            *self._planning_tools.values(),
        )

    @property
    def tool_names(self) -> frozenset[str]:
        """Client-tool names bound to research services and planning for this session."""
        return frozenset(self._bound) | frozenset(self._planning_tools)

    @property
    def policy_sha256(self) -> str:
        """Change whenever a bound tool is disabled, changed, disconnected or disallowed."""
        identities: list[object] = [
            self._manager.live_identity(item) for _, item in sorted(self._bound.items())
        ]
        if self._planning is not None:
            identities.append(
                {
                    "planning": self._planning.launch.model_dump(mode="json"),
                    "tools": [
                        item.model_dump(mode="json") for item in self._planning_tools.values()
                    ],
                }
            )
        return hashlib.sha256(
            json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def service_id(self, tool_name: str) -> str:
        """Return the service a bound client tool calls."""
        if tool_name in self._planning_tools:
            return PLANNING_SERVICE_ID
        return self._tool(tool_name).destination.service_id

    def _planner(self, tool_name: str) -> PlatformPlanning | None:
        """Return the planning state when the tool is a planning tool."""
        return self._planning if tool_name in self._planning_tools else None

    def _tool(self, tool_name: str) -> McpBoundTool:
        bound = self._bound.get(tool_name)
        if bound is None:
            raise ExternalActionError("worker tool has no research service binding")
        return bound

    def _context(self) -> str | None:
        return None if self._planning is None else self._planning.launch.project_context_id

    def _project(self, proposal: ExternalActionProposal) -> str:
        """Return the project a call names: the launch context's platform project, if any."""
        context = proposal.request.project_context_id
        if context is None:
            return str(proposal.worker.project_id)
        if self._planning is None or context != self._planning.launch.project_context_id:
            raise ExternalActionError("the operation belongs to another launch context")
        return self._planning.launch.project_id

    def prepare(self, tool_name: str, arguments: dict[str, JsonValue]) -> ExternalActionRequest:
        """Bind the worker's exact arguments to the server.

        A submission to an operation service also asks the service to describe it, which
        has no effect at the service, and records the estimate it refers to. The fields
        OpenHands adds to every call are left out unless the tool declares them. A planning
        call is checked against the launch context and contacts nothing; a refused one
        opens a review of its refusal.
        """
        planning = self._planner(tool_name)
        if planning is not None:
            spec = self._planning_tools[tool_name]
            summary, request_json = planning.agent_request(
                tool_name, _tool_arguments(arguments, set(spec.parameters["properties"]))
            )
            return ExternalActionRequest(
                service_id=PLANNING_SERVICE_ID,
                tool_name=tool_name,
                destination="heartwood-gateway",
                summary=summary,
                request_json=request_json,
                project_context_id=planning.launch.project_context_id,
            )
        bound = self._tool(tool_name)
        schema = worker_parameters(bound.tool.parsed)
        properties = set(schema["properties"])
        arguments = _tool_arguments(arguments, properties)
        if (
            GATEWAY_FIELDS & set(arguments)
            or not set(arguments) <= properties
            or not set(schema["required"]) <= set(arguments)
        ):
            raise ExternalActionError("arguments do not match the reviewed tool definition")
        parsed = urlsplit(bound.server.url)
        destination = (parsed.hostname or bound.server.server_id) + (
            f":{parsed.port}" if parsed.port is not None else ""
        )
        sent = dict(arguments)
        target: UUID | None = None
        review: ExternalOperationReview | None = None
        if bound.destination.cancels(bound.tool.name):
            target = _target(sent.pop(TARGET_FIELD, None))
            review = ExternalOperationReview(kind="cancel")
        elif bound.destination.operations is not None:
            proposal = self._proposal(bound, sent)
            review = ExternalOperationReview(
                kind="submit", proposal=proposal, estimate=self._estimate(proposal)
            )
        return ExternalActionRequest(
            service_id=bound.destination.service_id,
            tool_name=tool_name,
            destination=destination,
            summary=(
                f"Cancel operation {target} on {bound.server.label}"
                if target is not None
                else f"Call {bound.tool.name} on {bound.server.label}"
            )[:512],
            request_json=canonical_request(json.dumps(sent)),
            project_context_id=self._context(),
            target_operation_id=target,
            operation=review,
        )

    def _proposal(
        self, bound: McpBoundTool, arguments: dict[str, JsonValue]
    ) -> PlatformOperationProposal | None:
        project = (
            self._planning.launch.project_id
            if self._planning is not None
            else self._manager.project_id
        )
        if project is None:
            return None
        try:
            with self._manager.connect(bound.server, bound.destination) as client:
                return request_proposal(
                    client, bound.destination, bound.tool.name, project, arguments
                )
        except McpServerError:
            return None

    def _estimate(self, proposal: PlatformOperationProposal | None) -> ExternalOperationEstimate:
        if self._planning is None:
            return ExternalOperationEstimate(state="not-offered")
        if proposal is None or proposal.runtime_profile_ref is None:
            return ExternalOperationEstimate(
                state="missing", limitation="The platform named no runtime profile."
            )
        try:
            return self._planning.operation_estimate(proposal.runtime_profile_ref)
        except PlanningStateError:
            return ExternalOperationEstimate(
                state="missing", limitation="Planning records need integrity recovery."
            )

    def check_policy(
        self,
        proposal: ExternalActionProposal,
        actor_id: str,  # noqa: ARG002
        now: datetime,
    ) -> None:
        """Refuse dispatch once the server, its credential or the tool definition changed.

        A submission whose platform proposal is missing, or whose launch context changed,
        is refused too. Listing tools has no effect on the service; a refusal here keeps
        the review open and records no dispatch, so nothing is left with an unknown outcome.
        A planning call must name this session's launch context.
        """
        if self._planner(proposal.request.tool_name) is not None:
            if not proposal.created_at <= now < proposal.expires_at:
                raise ExternalActionError("outside proposal is expired")
            if proposal.request.project_context_id is None:
                raise ExternalActionError("a planning call names its launch context")
            self._project(proposal)
            return
        bound = self._tool(proposal.request.tool_name)
        self._manager.require_current(bound)
        if not proposal.created_at <= now < proposal.expires_at:
            raise ExternalActionError("outside proposal is expired")
        self._project(proposal)
        review = proposal.request.operation
        if review is not None and review.kind == "submit" and review.proposal is None:
            raise ExternalActionError("the platform returned no proposal for this operation")
        try:
            with self._manager.connect(bound.server, bound.destination) as client:
                self._verify_definition(client, bound)
        except McpServerError as error:
            raise ExternalActionError(error.detail) from None

    def read_input(self, reference: ExternalInputReference) -> bytes:  # noqa: ARG002
        """Research service calls carry no file inputs."""
        raise ExternalActionError("research service calls take no file inputs")

    def submit(
        self,
        proposal: ExternalActionProposal,
        decision: ExternalActionDecision,  # noqa: ARG002
        inputs: tuple[bytes, ...],  # noqa: ARG002
    ) -> ExternalActionObservation:
        """Verify the reviewed definition, then send the call once.

        A planning call runs once in the gateway's planning state.
        """
        planning = self._planner(proposal.request.tool_name)
        if planning is not None:
            self._project(proposal)
            return ExternalActionObservation(
                proposal_sha256=proposal.sha256,
                operation=planning.agent_call(
                    proposal.request.tool_name,
                    proposal.request.request_json,
                    operation_id=str(proposal.operation_id),
                ),
            )
        bound = self._tool(proposal.request.tool_name)
        self._manager.require_current(bound)
        arguments = json.loads(proposal.request.request_json)
        target = proposal.request.target_operation_id
        with self._manager.connect(bound.server, bound.destination) as client:
            self._verify_definition(client, bound)
            operation = self._call(
                client,
                bound,
                bound.tool.name,
                {
                    **arguments,
                    **({} if target is None else {TARGET_FIELD: str(target)}),
                    "project_id": self._project(proposal),
                    "operation_id": str(proposal.operation_id),
                },
            )
        return ExternalActionObservation(proposal_sha256=proposal.sha256, operation=operation)

    def observe(self, proposal: ExternalActionProposal) -> ExternalActionObservation:
        """Ask the service's read-only status tool about the existing operation identifier.

        A planning call whose outcome was not recorded is not repeated; a prediction is
        looked up in the journal, and the current planning overview is reported.
        """
        planning = self._planner(proposal.request.tool_name)
        if planning is not None:
            return ExternalActionObservation(
                proposal_sha256=proposal.sha256,
                operation=planning.agent_outcome(
                    proposal.request.tool_name,
                    proposal.request.request_json,
                    operation_id=str(proposal.operation_id),
                ),
            )
        bound = self._tool(proposal.request.tool_name)
        with self._manager.connect(bound.server, bound.destination) as client:
            operation = self._call(
                client,
                bound,
                bound.destination.observation_tool,
                {
                    "project_id": self._project(proposal),
                    "operation_id": str(proposal.operation_id),
                },
            )
        return ExternalActionObservation(proposal_sha256=proposal.sha256, operation=operation)

    def prepare_result(self, record: ExternalActionRecord) -> str:
        """Read the operation's current observation for a reviewed release.

        A planning call releases what it returned when it ran.
        """
        if self._planner(record.proposal.request.tool_name) is not None:
            if record.observation is None:
                raise ExternalActionError("the planning call has no result")
            return canonical_request(record.observation.operation.model_dump_json())
        observed = self.observe(record.proposal)
        return canonical_request(observed.operation.model_dump_json())

    def check_result_policy(
        self,
        record: ExternalActionRecord,
        content_json: str,
        model_route_json: str,  # noqa: ARG002
        actor_id: str,  # noqa: ARG002
        now: datetime,  # noqa: ARG002
    ) -> None:
        """Release only within the destination's size limit while the tool still holds."""
        if self._planner(record.proposal.request.tool_name) is not None:
            if len(content_json.encode()) > AGENT_RESULT_BYTES:
                raise ExternalActionError("the planning result exceeds its size limit")
            return
        bound = self._tool(record.proposal.request.tool_name)
        if self._manager.live_identity(bound) is None:
            raise ExternalActionError("the research service tool is no longer enabled")
        if len(content_json.encode()) > bound.destination.max_result_bytes:
            raise ExternalActionError("the research service result exceeds its size limit")

    @staticmethod
    def _verify_definition(client: MCPClient, bound: McpBoundTool) -> None:
        tools = cast(
            list[Any],
            client.call_async_from_sync(client.list_tools, timeout=_DISCOVERY_TIMEOUT_SECONDS),
        )
        current = next((tool for tool in tools if tool.name == bound.tool.name), None)
        if current is None or definition_digest(tool_definition(current)) != bound.tool.sha256:
            raise ExternalActionError("the research service tool changed after review")

    @staticmethod
    def _call(
        client: MCPClient, bound: McpBoundTool, name: str, arguments: dict[str, Any]
    ) -> PlatformOperationObservation:
        content = call_structured(client, bound.destination, name, arguments)
        return PlatformOperationObservation.model_validate_json(json.dumps(content))


# Fields OpenHands adds to every client tool's schema and keeps in the call's arguments.
_SDK_FIELDS = frozenset({"summary", "security_risk"})


def _tool_arguments(arguments: dict[str, JsonValue], declared: set[str]) -> dict[str, JsonValue]:
    """Return a call's arguments without the SDK's fields, unless the tool declares them."""
    return {
        key: value for key, value in arguments.items() if key in declared or key not in _SDK_FIELDS
    }


def _target(value: JsonValue | None) -> UUID:
    """Parse the operation a worker names; the gateway's records decide whether it exists."""
    try:
        if not isinstance(value, str):
            raise ValueError
        return UUID(value)
    except ValueError:
        raise ExternalActionError("a cancellation must name an operation identifier") from None


def advance_outside_calls(
    service: SessionService,
    adapter: McpOutsideAdapter,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Open outside review for new worker calls and result review for finished ones.

    Called after the gateway reconciles a session. Opening review of a call contacts
    nothing, except that a submission to an operation service asks the service to
    describe it. Observing operations and reading results contacts the service, so it
    runs at most once per interval. A refusal leaves the session as it was; the next
    read tries again. With planning, the operation records are then linked to it.
    """
    _advance(service, adapter, clock)
    if adapter.planning is not None:
        with suppress(ValueError):
            adapter.linked_sequence = link_operations(
                service, adapter.planning, after_sequence=adapter.linked_sequence
            )


def _advance(
    service: SessionService, adapter: McpOutsideAdapter, clock: Callable[[], float]
) -> None:
    try:
        _propose_calls(service, adapter)
        if clock() - adapter.last_reconciled < _RECONCILE_INTERVAL_SECONDS:
            return
        records = external_action_records(service.replay_events())
        settling = any(
            record.phase in {"submitting", "unknown"}
            or (record.observation is not None and not record.terminal)
            for record in records.values()
        )
        released = {
            release.proposal.operation_id
            for release in external_result_records(service.replay_events()).values()
            if release.phase != "expired"
        }
        finished = [
            record.proposal.operation_id
            for record in records.values()
            if record.terminal
            and record.observation is not None
            and record.proposal.request.tool_name in adapter.tool_names
            and record.proposal.operation_id not in released
        ]
        if not settling and not finished:
            return
        adapter.last_reconciled = clock()
        if settling:
            service.reconcile_external_actions()
            _propose_calls(service, adapter)
            released = {
                release.proposal.operation_id
                for release in external_result_records(service.replay_events()).values()
                if release.phase != "expired"
            }
            finished = [
                record.proposal.operation_id
                for record in external_action_records(service.replay_events()).values()
                if record.terminal
                and record.observation is not None
                and record.proposal.request.tool_name in adapter.tool_names
                and record.proposal.operation_id not in released
            ]
        for operation_id in finished:
            with suppress(ValueError):
                service.propose_external_result(operation_id)
    except ValueError:
        # Outside records, a closing session and a replaced worker refuse with ValueError.
        return


def _propose_calls(service: SessionService, adapter: McpOutsideAdapter) -> None:
    events = service.replay_events()
    records = external_action_records(events).values()
    reviewed = {record.proposal.source_event_id: record for record in records}
    # No review opens for a service with an unknown outcome, so a submission is not
    # described by its platform on every read while that outcome is reconciled.
    blocked = {
        record.proposal.request.service_id
        for record in records
        if record.phase in {"submitting", "unknown"}
    }
    attempt = service.worker_attempt()
    current = None if attempt is None else str(attempt.intent.attempt_id)
    for event in events:
        if (
            event.kind != EventKind.TOOL_CALL_PROPOSED
            or event.payload.get("tool_name") not in adapter.tool_names
            or event.payload.get("worker_attempt_id") != current
        ):
            continue
        existing = reviewed.get(event.event_id)
        if (existing is not None and existing.phase != "proposed") or adapter.service_id(
            str(event.payload.get("tool_name"))
        ) in blocked:
            continue
        # An expired review is closed and replaced; an open one is left alone.
        with suppress(ValueError):
            service.propose_external_action(event.event_id)
