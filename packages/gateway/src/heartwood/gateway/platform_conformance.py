# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Live conformance checks for a platform's inference and MCP endpoints.

The same checks run against a platform's own endpoints and against the deterministic
fakes in ``heartwood.gateway.platform_fakes``. Each capability is checked on its own,
so an unavailable inference route does not prevent the tool check and the reverse.
A passing check establishes contract shape and reachability for the supplied identity;
it does not establish authorization, policy enforcement, or live-platform qualification.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import openai
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import MCPError as McpProtocolError
from openai.types.chat import ChatCompletionToolParam
from openhands.sdk.event.error_classification import classify_exception
from openhands.sdk.mcp import MCPClient
from openhands.sdk.mcp.exceptions import MCPError

from heartwood.adapters.service_conformance import (
    ServiceConformanceError,
    validate_mcp_tools_list,
    validate_model_catalog,
)
from heartwood.core_adapter import BackendErrorCode
from heartwood.gateway._openhands_failures import classified_error_code
from heartwood.gateway._transport import require_protected_transport
from heartwood.schemas import PlatformMcpServiceProfile

_TIMEOUT_SECONDS = 15.0
_CHECK_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "synthetic_lookup",
        "description": "Look up a synthetic value.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}},
    },
}


@dataclass(frozen=True, slots=True)
class InferenceEndpoint:
    """An OpenAI-compatible base URL, the credential for it, and an optional model."""

    base_url: str
    api_key: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        """Refuse to send the key over an unprotected transport."""
        if self.api_key is not None:
            require_protected_transport(self.base_url)


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    """A platform MCP service profile and the bearer token the platform issued for it."""

    profile: PlatformMcpServiceProfile
    token: str | None = None

    def __post_init__(self) -> None:
        """Refuse to send the token over an unprotected transport."""
        if self.token is not None:
            require_protected_transport(str(self.profile.url))


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """The outcome of checking one capability; ``diagnostic`` names Heartwood's code."""

    capability: Literal["inference", "platform-tools"]
    target: str
    status: Literal["passed", "failed", "unavailable"]
    checks: tuple[str, ...]
    item_count: int = 0
    diagnostic: str | None = None


def check_inference(endpoint: InferenceEndpoint) -> CapabilityCheck:
    """List models and stream one tool-enabled Chat Completion from the endpoint.

    Endpoint errors are mapped to the diagnostic Heartwood shows for them, using the
    same failure classification as the agent runtime. Response content is not reported.
    """
    checks: list[str] = []
    client = openai.OpenAI(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key or "not-required",
        max_retries=0,
        timeout=_TIMEOUT_SECONDS,
    )
    try:
        page = client.models.list()
        report = validate_model_catalog(
            {"data": [model.model_dump(mode="json") for model in page.data]}
        )
        checks.extend(report.checks)
        model = endpoint.model or (page.data[0].id if page.data else None)
        if model is None:
            return CapabilityCheck("inference", "inference", "passed", tuple(checks), 0)
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Return one short synthetic result."}],
            tools=[_CHECK_TOOL],
            stream=True,
        )
        produced = finished = False
        for chunk in stream:
            for choice in chunk.choices:
                produced = produced or bool(choice.delta.content or choice.delta.tool_calls)
                finished = finished or choice.finish_reason is not None
        if not (produced and finished):
            raise ServiceConformanceError("stream ended without output and a finish reason")
        checks.append("Streaming Chat Completion with a tool definition and a finish reason")
        return CapabilityCheck("inference", "inference", "passed", tuple(checks), report.item_count)
    except ServiceConformanceError:
        return CapabilityCheck("inference", "inference", "failed", tuple(checks))
    except openai.APIError as error:
        code = classified_error_code(
            classify_exception(error),
            fallback=BackendErrorCode.PROVIDER_UNAVAILABLE,
        )
        return CapabilityCheck("inference", "inference", "unavailable", tuple(checks), 0, code)
    finally:
        client.close()


def check_mcp_service(endpoint: McpEndpoint) -> CapabilityCheck:
    """Connect over Streamable HTTP, check the negotiated revision, and list the tools."""
    service = endpoint.profile.service_id
    checks: list[str] = []
    client = MCPClient(  # type: ignore[no-untyped-call]
        StreamableHttpTransport(str(endpoint.profile.url), auth=endpoint.token),
        timeout=_TIMEOUT_SECONDS,
    )
    try:
        client.call_async_from_sync(client.connect, timeout=_TIMEOUT_SECONDS)
        initialized = client.initialize_result
        if (
            initialized is None
            or initialized.protocol_version not in endpoint.profile.supported_protocol_versions
        ):
            raise ServiceConformanceError("negotiated revision is not in the service profile")
        checks.append("Negotiated MCP revision listed in the service profile")
        result = client.call_async_from_sync(client.list_tools_mcp, timeout=_TIMEOUT_SECONDS)
        report = validate_mcp_tools_list(result.model_dump(mode="json", by_alias=True))
        checks.extend(report.checks)
        return CapabilityCheck(
            "platform-tools", service, "passed", tuple(checks), report.item_count
        )
    except ServiceConformanceError:
        return CapabilityCheck("platform-tools", service, "failed", tuple(checks))
    except (MCPError, McpProtocolError, OSError, TimeoutError):
        # Transport and authentication failures carry server text; report only the outcome.
        return CapabilityCheck("platform-tools", service, "unavailable", tuple(checks))
    finally:
        client.sync_close()


def run_platform_conformance(
    inference: InferenceEndpoint | None, services: Sequence[McpEndpoint] = ()
) -> tuple[CapabilityCheck, ...]:
    """Check each supplied capability independently and return every outcome."""
    return (
        *(() if inference is None else (check_inference(inference),)),
        *(check_mcp_service(service) for service in services),
    )


__all__ = [
    "CapabilityCheck",
    "InferenceEndpoint",
    "McpEndpoint",
    "check_inference",
    "check_mcp_service",
    "run_platform_conformance",
]
