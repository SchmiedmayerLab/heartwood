# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Platform model-connection manifest structure, without credentials or runtime policy."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_NonEmptyString = Annotated[str, Field(min_length=1)]
# Rust's $ is an absolute end anchor. JSON Schema engines may also match it before
# a trailing newline; the emitted patterns use an absolute-end lookahead instead.


class ModelConnectionManifest(BaseModel):
    """One platform connection; the gateway additionally validates route and credential rules.

    Structural validation does not authorize an endpoint or resolve a credential.
    The gateway checks endpoint form, credential-reference consistency, protocol-specific
    requirements, and uniqueness against built-in connections before accepting this record.
    These checks do not qualify transport security or credential isolation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    connection_id: _NonEmptyString = Field(
        pattern=r"^[A-Za-z0-9_-]+$",
        json_schema_extra={"pattern": r"^[A-Za-z0-9_-]+(?![\s\S])"},
        description="Unique connection identifier.",
    )
    label: _NonEmptyString = Field(pattern=r"\S", description="Researcher-facing connection name.")
    protocol: Literal["anthropic", "openai", "openai-compatible", "static", "subscription"]
    model_prefix: _NonEmptyString = Field(
        pattern=r"/$",
        json_schema_extra={"pattern": r"/(?![\s\S])"},
        description="Execution provider prefix ending in '/'.",
    )
    source: Literal["platform"] = "platform"
    credential_kind: Literal["environment", "file", "managed-identity", "none"]
    policy_endpoint: _NonEmptyString | None = Field(
        default=None, description="Model-request endpoint; required by gateway validation."
    )
    catalog_endpoint: _NonEmptyString | None = Field(
        default=None,
        description="Model-discovery endpoint; optional for static/subscription routes.",
    )
    base_url: _NonEmptyString | None = None
    api_key_env: _NonEmptyString | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        json_schema_extra={"pattern": r"^[A-Za-z_][A-Za-z0-9_]*(?![\s\S])"},
        description="Environment variable name, never the API key value.",
    )
    api_key_file: _NonEmptyString | None = Field(
        default=None, description="Absolute mounted credential-file path, never its contents."
    )
    api_version: _NonEmptyString | None = Field(default=None, pattern=r"\S")
    aws_region_name: _NonEmptyString | None = Field(default=None, pattern=r"\S")
    aws_profile_name: _NonEmptyString | None = Field(default=None, pattern=r"\S")
    description: str = ""
    static_models: list[
        Annotated[
            str,
            Field(
                pattern=r"^\S+$",
                json_schema_extra={"pattern": r"^\S+(?![\s\S])"},
            ),
        ]
    ] = Field(default_factory=list, description="Model identifiers for static connections only.")
    subscription_vendor: _NonEmptyString | None = Field(
        default=None,
        description="Subscription provider; validated by the gateway for this protocol.",
    )


class ModelConnectionsManifest(BaseModel):
    """Versioned structural input to the platform model-connection loader.

    Passing this schema alone does not establish policy authorization, successful model
    discovery, or availability of the deployment's credential and inference services.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal["heartwood.model-connections.v1"]
    connections: list[ModelConnectionManifest]
