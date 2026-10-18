# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Draft platform metadata supplements; structural validity is never authorization.

Inference requests and MCP messages retain their upstream schemas. These records supply
only launch bindings, missing model declarations, and non-secret deployment references.
No loader, credential resolution, network connection, or policy enforcement is provided.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

type DataClassification = Annotated[
    Literal["public", "internal", "confidential", "restricted"],
    Field(
        description=(
            "Common classification vocabulary. Values are exact labels, not an ordered access "
            "hierarchy. Regulatory descriptors are separate; no label grants permission."
        )
    ),
]
_Reference = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        pattern=r"^\S+$",
        json_schema_extra={"pattern": r"^\S+(?![\s\S])"},
    ),
]
_Description = Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")]
_PositiveTokens = Annotated[int, Field(gt=0)]
_ENDPOINT_PATTERN = r"^[hH][tT][tT][pP][sS]?://[^/?#@\s\\]+(?:/[^?#\s\\]*)?(?![\s\S])"


class _PlatformMetadata(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class PlatformLaunchContext(_PlatformMetadata):
    """A platform-issued context binding, separate from Heartwood's local project.

    The process working directory remains the local project boundary. The platform's
    project and billing references do not replace it and cannot be selected by an agent.
    Parsing this record does not authenticate its issuer or activate a billing binding.
    """

    schema_version: Literal["heartwood.platform-launch-context.v1"] = (
        "heartwood.platform-launch-context.v1"
    )
    project_context_id: _Reference = Field(
        description="Opaque launch-context identity used by estimates and operation requests."
    )
    platform_id: _Reference
    project_id: _Reference = Field(description="Platform authorization/resource project identity.")
    billing_attribution_ref: _Reference | None = Field(
        default=None,
        description=(
            "Opaque platform-owned billing/grant binding; never account details or a secret."
        ),
    )
    authority_ref: _Reference = Field(description="Platform authority that issued this context.")
    policy_ref: _Reference = Field(description="Exact applicable platform policy reference.")
    issued_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_expiry(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")
        return self


class DataEligibilityObservation(_PlatformMetadata):
    """One sourced eligibility declaration for one exact data class and enclosing route.

    An observed allowance is not an authorization token, compliance certification, or
    permission for other classes, modalities, model identities, or later operations.
    Null means unknown. Missing classes are also unknown, never implicitly allowed.
    """

    classification: DataClassification
    allowed: bool | None = Field(description="Observed platform declaration; null means unknown.")
    regulatory_descriptors: tuple[_Description, ...] = Field(
        default=(),
        description="Independent descriptive labels; neither classification nor authorization.",
    )
    authority_ref: _Reference
    policy_ref: _Reference = Field(description="Exact policy version governing this declaration.")
    source_ref: _Reference = Field(description="Reference to the declaration's source evidence.")
    observed_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_observation(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.observed_at:
            raise ValueError("expires_at must follow observed_at")
        if len(set(self.regulatory_descriptors)) != len(self.regulatory_descriptors):
            raise ValueError("regulatory_descriptors must be unique")
        return self


class PlatformModelMetadata(_PlatformMetadata):
    """Missing model declarations keyed by an exact connection and upstream model ID.

    This supplements the upstream model catalog; it does not replace catalog identity,
    lifecycle fields, provider requests, SDK capability lookup, or route authorization.
    Unknown limits/support remain null. Eligibility applies only to this exact route.
    """

    connection_id: _Reference
    model_id: _Reference
    protocols: tuple[_Reference, ...] = Field(
        min_length=1,
        description="Existing provider/SDK protocol identifiers; not a new inference wire format.",
    )
    capability_profile_ref: _Reference | None = Field(
        default=None, description="Optional versioned provider/SDK capability profile reference."
    )
    context_window_tokens: _PositiveTokens | None = None
    max_input_tokens: _PositiveTokens | None = None
    max_output_tokens: _PositiveTokens | None = None
    supports_tool_calls: bool | None = None
    data_eligibility: tuple[DataEligibilityObservation, ...] = ()

    @model_validator(mode="after")
    def _validate_metadata(self) -> Self:
        if len(set(self.protocols)) != len(self.protocols):
            raise ValueError("protocols must be unique")
        classes = [observation.classification for observation in self.data_eligibility]
        if len(set(classes)) != len(classes):
            raise ValueError("data_eligibility must contain at most one observation per class")
        if self.context_window_tokens is not None and any(
            limit is not None and limit > self.context_window_tokens
            for limit in (self.max_input_tokens, self.max_output_tokens)
        ):
            raise ValueError("input/output token limits cannot exceed a known context window")
        return self


class PlatformModelMetadataManifest(_PlatformMetadata):
    """Versioned model-catalog supplement; an empty catalog makes no availability claim."""

    schema_version: Literal["heartwood.platform-model-metadata.v1"] = (
        "heartwood.platform-model-metadata.v1"
    )
    models: tuple[PlatformModelMetadata, ...] = ()

    @model_validator(mode="after")
    def _validate_identities(self) -> Self:
        identities = [(model.connection_id, model.model_id) for model in self.models]
        if len(set(identities)) != len(identities):
            raise ValueError("connection_id/model_id pairs must be unique")
        return self


class McpDiscoveryObservation(_PlatformMetadata):
    """A sourced observation of upstream MCP initialization, not a capability grant.

    The client must negotiate capabilities again as required by the upstream protocol.
    Optional extensions, including Tasks, retain their own revision and capability
    definitions; this record defines no task states, request bodies, or cancellation.
    """

    protocol_version: _Reference = Field(description="Exact negotiated upstream MCP revision.")
    capabilities: tuple[_Reference, ...] = Field(
        description="Observed upstream capability identifiers, not copied wire capability objects."
    )
    extension_refs: tuple[_Reference, ...] = Field(
        default=(), description="Exact upstream extension revisions observed during negotiation."
    )
    source_ref: _Reference
    observed_at: AwareDatetime

    @field_validator("capabilities", "extension_refs")
    @classmethod
    def _validate_unique_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("capability and extension references must be unique")
        return values


class PlatformMcpServiceProfile(_PlatformMetadata):
    """Non-secret deployment profile for the SDK's remote MCP server configuration.

    ``url`` and ``transport`` retain the existing SDK configuration vocabulary. A trusted
    adapter resolves ``credential_ref`` into the SDK's authentication configuration;
    headers, OAuth tokens, environment values, and local subprocess commands are absent.
    This profile cannot enable a service or prove its annotations, safety, or authority.
    """

    schema_version: Literal["heartwood.platform-mcp-service.v1"] = (
        "heartwood.platform-mcp-service.v1"
    )
    service_id: _Reference
    url: AnyHttpUrl = Field(
        description="Deployment-owned MCP endpoint; no credentials, query, or fragment.",
        json_schema_extra={"pattern": _ENDPOINT_PATTERN},
    )
    transport: Literal["streamable-http"] = "streamable-http"
    credential_ref: _Reference | None = Field(
        default=None,
        description=(
            "Deployment credential binding name, never the credential value; "
            "null makes no authentication claim."
        ),
    )
    authority_ref: _Reference
    policy_ref: _Reference
    supported_protocol_versions: tuple[_Reference, ...] = Field(
        min_length=1, description="Exact upstream MCP revisions this deployment supports."
    )
    required_capabilities: tuple[_Reference, ...] = Field(
        default=(), description="Upstream capability identifiers required before using the service."
    )
    discovery: McpDiscoveryObservation | None = None

    @field_validator("url", mode="before")
    @classmethod
    def _validate_url_text(cls, value: object) -> object:
        if isinstance(value, str) and re.fullmatch(_ENDPOINT_PATTERN, value) is None:
            raise ValueError(
                "url must be an explicit HTTP(S) endpoint without credentials or query"
            )
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if any(
            part is not None
            for part in (value.username, value.password, value.query, value.fragment)
        ):
            raise ValueError("url cannot contain credentials, a query, or a fragment")
        return value

    @field_validator("supported_protocol_versions", "required_capabilities")
    @classmethod
    def _validate_unique_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("protocol versions and required capabilities must be unique")
        return values

    @model_validator(mode="after")
    def _validate_negotiated_revision(self) -> Self:
        if (
            self.discovery is not None
            and self.discovery.protocol_version not in self.supported_protocol_versions
        ):
            raise ValueError("observed protocol version must be supported by the profile")
        return self
