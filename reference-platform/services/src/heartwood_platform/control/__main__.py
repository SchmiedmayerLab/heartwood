# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Entry point of the control service: python -m heartwood_platform.control CONFIGURATION."""

from __future__ import annotations

import logging
import ssl
import sys
from pathlib import Path
from typing import Annotated, Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.applications import Starlette

from heartwood_platform.common.kubernetes import Kubernetes, in_cluster_client
from heartwood_platform.common.serving import TlsSettings, serve
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.control.app import ProjectId, Subject, create_app
from heartwood_platform.control.hosted import HostedGateway, HostedGatewaySettings, sandbox_digests
from heartwood_platform.control.keycloak import Keycloak
from heartwood_platform.control.launch import LaunchService
from heartwood_platform.control.prices import PriceSheet
from heartwood_platform.control.registry import Project, Registry
from heartwood_platform.control.sessions import ApiServerAddress, Sessions, SessionSettings

# The form of the references in a launch context.
Reference = Annotated[str, StringConstraints(pattern=r"^\S{1,512}$")]


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KeycloakSettings(_Settings):
    """How the control service reaches Keycloak."""

    url: str = Field(pattern=r"^https://[^/]+$")
    # The platform CA, the only authority the control service trusts for Keycloak.
    ca_file: Path
    issuer: str
    realm: str
    client_id: str
    client_secret_file: Path


class ProjectSettings(_Settings):
    """One project of the registry."""

    id: ProjectId
    billing_attribution_ref: Reference
    policy_ref: Reference
    members: frozenset[Subject] = frozenset()


class ApiServerSettings(_Settings):
    """An address under which pods reach the Kubernetes API server."""

    cidr: str
    port: int


class SessionNamespaceSettings(_Settings):
    """How the control service provisions session namespaces."""

    control_namespace: str
    control_account: str
    gateway_namespace: str
    openshell_objects_file: Path
    openshell_gateway_image: str
    api_server: tuple[ApiServerSettings, ...]
    quota: dict[str, str]
    container_defaults: dict[str, str]
    container_default_requests: dict[str, str]
    platform_ca_file: Path
    openshell_repository: str = ""


class Settings(_Settings):
    """The configuration file of the control service."""

    platform_id: Reference
    database: Path
    audience: str
    service_callers: frozenset[str]
    # Service accounts of platform operators, which may rotate project client secrets.
    operator_callers: frozenset[str] = frozenset()
    redirect_uri_template: str
    keycloak: KeycloakSettings
    # The serving certificate from the platform CA; the service serves only HTTPS.
    tls: TlsSettings
    # The files of each session's hosted gateway.
    hosted: HostedGatewaySettings
    sessions: SessionNamespaceSettings
    projects: tuple[ProjectSettings, ...]
    synthetic_prices: PriceSheet

    @model_validator(mode="after")
    def _validate(self) -> Self:
        identifiers = [project.id for project in self.projects]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("project identifiers must be unique")
        try:
            self.redirect_uri_template.format(subject="subject", project="project")
        except (IndexError, KeyError) as error:
            raise ValueError("redirect_uri_template has an unknown field") from error
        return self


def build(settings: Settings, kubernetes: Kubernetes) -> Starlette:
    """Assemble the application from its configuration."""
    registry = Registry(settings.database)
    registry.synchronize(
        Project(project.id, project.billing_attribution_ref, project.policy_ref, project.members)
        for project in settings.projects
    )
    client = httpx.Client(
        base_url=settings.keycloak.url,
        timeout=30,
        verify=ssl.create_default_context(cafile=settings.keycloak.ca_file),
    )
    secret_file = settings.keycloak.client_secret_file
    keycloak = Keycloak(
        client,
        settings.keycloak.realm,
        settings.keycloak.client_id,
        lambda: secret_file.read_text(encoding="utf-8").strip(),
    )

    def keys() -> dict[str, Any]:
        path = f"/realms/{settings.keycloak.realm}/protocol/openid-connect/certs"
        response = client.get(path)
        response.raise_for_status()
        document = response.json()
        assert isinstance(document, dict)
        return document

    namespaces = settings.sessions
    openshell_objects = namespaces.openshell_objects_file.read_text(encoding="utf-8")
    sessions = Sessions(
        kubernetes,
        SessionSettings(
            control_namespace=namespaces.control_namespace,
            control_account=namespaces.control_account,
            gateway_namespace=namespaces.gateway_namespace,
            openshell_objects=openshell_objects,
            openshell_gateway_image=namespaces.openshell_gateway_image,
            api_server=tuple(
                ApiServerAddress(address.cidr, address.port) for address in namespaces.api_server
            ),
            quota=namespaces.quota,
            container_defaults=namespaces.container_defaults,
            container_default_requests=namespaces.container_default_requests,
            platform_ca=namespaces.platform_ca_file.read_text(encoding="utf-8"),
            openshell_repository=namespaces.openshell_repository,
        ),
        registry.session_ids,
    )
    supervisor, sandbox = sandbox_digests(openshell_objects)
    hosted = HostedGateway(
        settings.hosted,
        issuer=settings.keycloak.issuer,
        supervisor=supervisor,
        sandbox=sandbox,
    )
    launches = LaunchService(
        registry,
        keycloak,
        sessions,
        settings.platform_id,
        settings.redirect_uri_template,
        hosted=hosted,
    )
    return create_app(
        launches,
        ResearcherTokens(settings.keycloak.issuer, settings.audience, keys),
        ServiceTokens(kubernetes.review_token, settings.audience, settings.service_callers),
        settings.synthetic_prices,
        operators=ServiceTokens(
            kubernetes.review_token, settings.audience, settings.operator_callers
        )
        if settings.operator_callers
        else None,
    )


def main() -> None:
    """Serve the control service with the configuration file named on the command line."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = Settings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    application = build(settings, Kubernetes(in_cluster_client()))
    serve(application, settings.tls)


if __name__ == "__main__":
    main()
