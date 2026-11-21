# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deployment destinations, researcher settings and checks for research MCP servers.

The deployment manifest lists the exact origins the gateway may connect to and, for each,
the tools a researcher may select. Project configuration names servers and holds the
reviewed definition of each selected tool. It never holds a credential or a credential
reference: the gateway derives the binding name from the server identifier and resolves
it through its credential store.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from heartwood.gateway._diagnostics import diagnostic_for

# The service learns the project and operation identity from the gateway, never the worker.
GATEWAY_FIELDS = frozenset({"project_id", "operation_id"})
# A cancellation's target, which the worker names and the gateway takes from its own record.
TARGET_FIELD = "target_operation_id"
_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_SERVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_RESULT_BYTES = 32 * 1024
_MAX_WORKER_TOOL_NAME = 64
# RFC 1918 and unique local addresses, which cluster service networks use.
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(item)
    for item in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
# Cloud instance metadata services outside link-local ranges.
_METADATA_NETWORKS = (ipaddress.ip_network("fd00:ec2::/32"),)
# Prefixes whose last 32 bits carry an IPv4 address: NAT64, IPv4-compatible and
# IPv4-translated (RFC 2765).
_EMBEDDED_IPV4 = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("::/96"),
    ipaddress.ip_network("::ffff:0:0:0/96"),
)

type McpAuthenticationStatus = Literal["missing", "valid", "expired", "unknown-expiry"]


class McpServerError(ValueError):
    """A refused research service operation, reported with its stable diagnostic code."""

    def __init__(self, check_id: str, message: str) -> None:
        self.code = diagnostic_for(check_id).code
        self.detail = message
        super().__init__(f"{self.code}: {message}")


class McpOperationProfile(BaseModel):
    """The tools that make a destination a platform operation service.

    Every selectable tool of such a destination submits work, except the cancellation
    tool. The capability and proposal tools are read-only and not selectable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capabilities_tool: str = Field(pattern=_TOOL_NAME.pattern)
    proposal_tool: str = Field(pattern=_TOOL_NAME.pattern)
    cancel_tool: str | None = Field(default=None, pattern=_TOOL_NAME.pattern)


class McpDestination(BaseModel):
    """One deployment-approved origin and the tools researchers may select there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service_id: str = Field(pattern=_SERVICE_ID.pattern)
    origin: str
    tools: tuple[str, ...] = Field(min_length=1)
    observation_tool: str = Field(pattern=_TOOL_NAME.pattern)
    max_result_bytes: int = Field(default=16 * 1024, ge=1024, le=_MAX_RESULT_BYTES)
    call_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    # The operator's opt-in for a cluster or other private address at this exact origin.
    private_address: bool = False
    operations: McpOperationProfile | None = None

    @field_validator("origin")
    @classmethod
    def _exact_origin(cls, value: str) -> str:
        return exact_origin(value, private_address=True)

    @field_validator("tools")
    @classmethod
    def _tool_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            _TOOL_NAME.fullmatch(name) is None for name in value
        ):
            raise ValueError("tools must be unique MCP tool names")
        return value

    @model_validator(mode="after")
    def _observation_is_separate(self) -> Self:
        if self.observation_tool in self.tools:
            raise ValueError("the observation tool cannot also be a selectable tool")
        if self.operations is not None:
            readers = (self.operations.capabilities_tool, self.operations.proposal_tool)
            if len({*readers, self.observation_tool}) != 3 or set(readers) & set(self.tools):
                raise ValueError(
                    "the capability, proposal and observation tools must be distinct and "
                    "not selectable"
                )
            if self.operations.cancel_tool is not None and (
                self.operations.cancel_tool not in self.tools or len(self.tools) < 2
            ):
                raise ValueError(
                    "the cancellation tool must be selectable next to a submission tool"
                )
        exact_origin(self.origin, private_address=self.private_address)
        return self

    def cancels(self, tool: str) -> bool:
        """Return whether ``tool`` is this destination's cancellation tool."""
        return self.operations is not None and self.operations.cancel_tool == tool


def mcp_destinations_from_mapping(value: object) -> tuple[McpDestination, ...]:
    """Validate the manifest's destination list; one origin maps to one service."""
    if not isinstance(value, list):
        raise ValueError("mcp_destinations must be an array of tables")
    try:
        destinations = tuple(McpDestination.model_validate(item) for item in value)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'destination'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise ValueError(f"invalid MCP destination: {details}") from None
    origins = [item.origin for item in destinations]
    if len(origins) != len(set(origins)):
        raise ValueError("each MCP destination origin must be listed once")
    bindings = [mcp_credential_binding(item.service_id) for item in destinations]
    if len(bindings) != len(set(bindings)):
        raise ValueError("MCP destination service identifiers must have distinct credential names")
    return destinations


def exact_origin(value: str, *, private_address: bool = False) -> str:
    """Return a normalized origin, refusing paths, credentials and unsafe addresses."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("origin has an invalid port") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin must be a scheme, host and optional port only")
    check_host(parsed.scheme, host, private_address=private_address)
    return _render_origin(parsed.scheme, host, port)


def _render_origin(scheme: str, host: str, port: int | None) -> str:
    rendered = f"[{host}]" if ":" in host else host
    default = {"http": 80, "https": 443}[scheme]
    return f"{scheme}://{rendered}" + (f":{port}" if port not in {None, default} else "")


def check_host(scheme: str, host: str, *, private_address: bool = False) -> None:
    """Refuse plain HTTP beyond loopback and literal private, link-local or metadata hosts.

    With the deployment's opt-in, a private address is permitted; link-local and metadata
    addresses never are.
    """
    loopback = host in _LOOPBACK_HOSTS or _is_loopback_literal(host)
    if scheme != "https" and not loopback:
        raise ValueError("origins other than loopback must use HTTPS")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not loopback and not _permitted(address, private_address=private_address):
        raise ValueError(
            "origins on private addresses need the destination's opt-in, and link-local "
            "or metadata addresses are refused"
        )


def check_resolved_host(host: str, port: int, *, private_address: bool = False) -> None:
    """Resolve the host and refuse it unless every address is permitted, or all are loopback.

    Permitted addresses are public ones and, with the destination's opt-in, private ones.

    The check runs before each connection; the platform's egress controls still apply to
    the connection the client then opens.
    """
    try:
        resolved = {
            ipaddress.ip_address(str(item[4][0]).split("%", 1)[0])
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        raise McpServerError(
            "mcp-unavailable", "the research service could not be resolved"
        ) from None
    if not resolved:
        raise McpServerError("mcp-unavailable", "the research service could not be resolved")
    loopback = host in _LOOPBACK_HOSTS or _is_loopback_literal(host)
    if loopback:
        if not all(_loopback(address) for address in resolved):
            raise McpServerError("mcp-destination", "a loopback origin resolved beyond loopback")
        return
    if not all(_permitted(address, private_address=private_address) for address in resolved):
        raise McpServerError(
            "mcp-destination",
            "the research service resolved to a private, link-local or metadata address "
            "the destination does not permit",
        )


def _is_loopback_literal(host: str) -> bool:
    try:
        return _loopback(ipaddress.ip_address(host))
    except ValueError:
        return False


def _loopback(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Treat only literal and IPv4-mapped loopback as loopback; translated forms leave the host."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _unmapped(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address not in {ipaddress.IPv6Address("::"), ipaddress.IPv6Address("::1")} and any(
            address in network for network in _EMBEDDED_IPV4
        ):
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def _permitted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, private_address: bool
) -> bool:
    address = _unmapped(address)
    if (
        any(address in network for network in _METADATA_NETWORKS)
        or address.is_link_local
        or address.is_multicast
    ):
        return False
    if address.is_global:
        return True
    return private_address and any(address in network for network in _PRIVATE_NETWORKS)


def server_url(value: str) -> str:
    """Validate a researcher-entered Streamable HTTP endpoint without contacting it."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(value) > 512
    ):
        raise McpServerError(
            "mcp-destination",
            "the server URL must be an HTTP(S) endpoint without credentials, query or fragment",
        )
    return value.strip()


def url_origin(url: str) -> str:
    """Return the origin of a validated server URL."""
    parsed = urlsplit(url)
    return _render_origin(parsed.scheme, (parsed.hostname or "").lower(), parsed.port)


def destination_for(url: str, destinations: Sequence[McpDestination]) -> McpDestination:
    """Return the deployment destination for a server URL, or refuse the URL."""
    if not destinations:
        raise McpServerError(
            "mcp-destination", "the deployment allows no research service destinations"
        )
    origin = url_origin(server_url(url))
    destination = next((item for item in destinations if item.origin == origin), None)
    if destination is None:
        raise McpServerError(
            "mcp-destination", f"{origin} is not a deployment-approved research service origin"
        )
    return destination


def tool_definition(tool: BaseModel) -> str:
    """Return the canonical reviewed definition of one discovered MCP tool."""
    value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    for key in ("_meta", "meta", "icons"):
        value.pop(key, None)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise McpServerError(
            "mcp-unavailable", "the research service listed a tool definition that is not JSON"
        ) from None


def definition_digest(definition: str) -> str:
    """Return the digest that a renewed review must match."""
    return hashlib.sha256(definition.encode()).hexdigest()


def binding_supported(definition: Mapping[str, Any], *, cancel: bool = False) -> bool:
    """Return whether a tool accepts the gateway's project and operation identity.

    A cancellation tool must also require the identifier of the operation it cancels.
    """
    schema = definition.get("inputSchema")
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    required = schema.get("required")
    fields = GATEWAY_FIELDS | ({TARGET_FIELD} if cancel else set())
    return (
        isinstance(properties, dict)
        and isinstance(required, list)
        and set(properties) >= fields
        and set(required) >= fields
    )


def worker_parameters(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Return the input schema the worker sees, without gateway-supplied fields."""
    schema = dict(definition.get("inputSchema") or {})
    properties = schema.get("properties")
    schema["properties"] = {
        key: value
        for key, value in (properties.items() if isinstance(properties, dict) else ())
        if key not in GATEWAY_FIELDS
    }
    required = schema.get("required")
    schema["required"] = [
        key for key in (required if isinstance(required, list) else ()) if key not in GATEWAY_FIELDS
    ]
    schema["type"] = "object"
    return schema


@dataclass(frozen=True, slots=True)
class McpToolSelection:
    """One researcher-selected tool and the definition the researcher reviewed."""

    name: str
    definition: str

    def validate(self) -> None:
        """Require a canonical definition naming this tool."""
        if _TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError(f"unsupported MCP tool name: {self.name}")
        try:
            parsed = json.loads(self.definition)
        except json.JSONDecodeError:
            raise ValueError("MCP tool definition must be JSON") from None
        if (
            not isinstance(parsed, dict)
            or parsed.get("name") != self.name
            or json.dumps(parsed, sort_keys=True, separators=(",", ":")) != self.definition
        ):
            raise ValueError("MCP tool definition must be the canonical definition of the tool")

    @property
    def parsed(self) -> dict[str, Any]:
        """Return the reviewed definition."""
        value: dict[str, Any] = json.loads(self.definition)
        return value

    @property
    def sha256(self) -> str:
        """Return the digest of the reviewed definition."""
        return definition_digest(self.definition)


@dataclass(frozen=True, slots=True)
class McpServerSetting:
    """One researcher-configured server; no credential or credential reference."""

    server_id: str
    label: str
    url: str
    enabled: bool = False
    tools: tuple[McpToolSelection, ...] = ()

    def validate(self) -> None:
        """Validate identity, endpoint syntax and tool selections."""
        if _SERVER_ID.fullmatch(self.server_id) is None:
            raise ValueError(
                "MCP server identifiers use up to 24 lowercase letters, digits and hyphens"
            )
        if not self.label.strip() or len(self.label) > 80:
            raise ValueError("MCP server label must be 1 to 80 characters")
        try:
            server_url(self.url)
        except McpServerError as error:
            raise ValueError(error.detail) from None
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("MCP tool selections must be unique")
        for tool in self.tools:
            tool.validate()
            if len(worker_tool_name(self.server_id, tool)) > _MAX_WORKER_TOOL_NAME:
                raise ValueError("MCP server and tool names are too long together")


def worker_tool_name(server_id: str, tool: McpToolSelection) -> str:
    """Return the client-tool name; a changed definition gets a new, stable name."""
    return f"{server_id.replace('-', '_')}__{tool.name}_{tool.sha256[:8]}"


def mcp_credential_binding(service_id: str) -> str:
    """Return the binding for a destination's bearer token.

    The binding follows the deployment's service identity, so a token entered or
    provided for one approved service is never sent to another approved origin.
    """
    return "HEARTWOOD_MCP_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", service_id).upper()


def mcp_server_from_mapping(value: object) -> McpServerSetting:
    """Validate one TOML-decoded server table."""
    if not isinstance(value, dict) or set(value) - {
        "server_id",
        "label",
        "url",
        "enabled",
        "tools",
    }:
        raise ValueError("mcp_servers entries contain unsupported fields")
    tools = value.get("tools", [])
    if not isinstance(tools, list) or any(
        not isinstance(item, dict) or set(item) != {"name", "definition"} for item in tools
    ):
        raise ValueError("mcp_servers tools must be tables with name and definition")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("mcp_servers enabled must be true or false")
    for key in ("server_id", "label", "url"):
        if not isinstance(value.get(key), str):
            raise ValueError(f"mcp_servers {key} must be a string")
    setting = McpServerSetting(
        server_id=value["server_id"],
        label=value["label"],
        url=value["url"],
        enabled=enabled,
        tools=tuple(
            McpToolSelection(name=str(item["name"]), definition=str(item["definition"]))
            for item in tools
        ),
    )
    setting.validate()
    return setting


def mcp_server_mapping(setting: McpServerSetting) -> dict[str, object]:
    """Return the TOML representation of one server."""
    return {
        "server_id": setting.server_id,
        "label": setting.label,
        "url": setting.url,
        "enabled": setting.enabled,
        "tools": [{"name": tool.name, "definition": tool.definition} for tool in setting.tools],
    }


@dataclass(frozen=True, slots=True)
class McpAuthentication:
    """Authentication state judged by the gateway from the token it holds."""

    status: McpAuthenticationStatus
    expires_at: str | None = None

    def safe_dict(self) -> dict[str, object]:
        """Return the state without the token."""
        return {"status": self.status, "expires_at": self.expires_at}


def token_authentication(token: str | None, now: datetime | None = None) -> McpAuthentication:
    """Judge expiry from a JWT's ``exp`` claim without trusting it as authorization.

    The service verifies the token; the gateway reads the claim only to refuse an expired
    token before it is sent and to show when the researcher must renew it.
    """
    if not token:
        return McpAuthentication("missing")
    parts = token.split(".")
    if len(parts) != 3:
        return McpAuthentication("unknown-expiry")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode()))
        expiry = claims.get("exp") if isinstance(claims, dict) else None
        if not isinstance(expiry, int | float) or isinstance(expiry, bool):
            return McpAuthentication("unknown-expiry")
        expires = datetime.fromtimestamp(expiry, UTC)
    except (binascii.Error, ValueError, OverflowError, OSError, UnicodeDecodeError):
        return McpAuthentication("unknown-expiry")
    current = now or datetime.now(UTC)
    return McpAuthentication(
        "expired" if expires <= current else "valid",
        expires.isoformat().replace("+00:00", "Z"),
    )
