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
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.applications import Starlette

from heartwood_platform.common.kubernetes import Kubernetes, in_cluster_client
from heartwood_platform.common.serving import TlsSettings, serve
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.control.app import ProjectId, create_app
from heartwood_platform.control.budgets import Allocation, Category, Ledger
from heartwood_platform.control.gateway_certificates import GatewayCertificates
from heartwood_platform.control.hosted import (
    WORKER_WORKSPACE,
    HostedGateway,
    HostedGatewaySettings,
    sandbox_digests,
)
from heartwood_platform.control.inference import (
    InferenceSetupError,
    ManagedInference,
    RouteModel,
    request_headroom,
)
from heartwood_platform.control.keycloak import Keycloak
from heartwood_platform.control.launch import LaunchService
from heartwood_platform.control.litellm import LiteLLM
from heartwood_platform.control.openshell import OpenShellProviders, ProviderRoute
from heartwood_platform.control.prices import PriceSheet
from heartwood_platform.control.project_data import ProjectData, ProjectDataSettings
from heartwood_platform.control.registry import Project, Registry
from heartwood_platform.control.researchers import Subject, read_key, researcher_id
from heartwood_platform.control.sessions import ApiServerAddress, Sessions, SessionSettings

# The form of the references in a launch context.
Reference = Annotated[str, StringConstraints(pattern=r"^\S{1,512}$")]
# An amount in the denomination of the price sheet, with at most 18 integer and 12
# fractional digits.
Amount = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,17})(\.[0-9]{1,12})?$")]


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
    # The amounts allocated to each category of the project's work.
    budgets: dict[Category, Amount] = Field(default_factory=dict)


class RouteModelSettings(_Settings):
    """A model of the route, named like its runtime profile in the price sheet."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    max_input_tokens: int = Field(gt=0, le=10_000_000)
    max_output_tokens: int = Field(gt=0, le=10_000_000)
    supports_tool_calls: bool = False


class InferenceSettings(_Settings):
    """The model route of sessions and how much of its budget each session reserves."""

    session_reservation: Amount
    # The LiteLLM proxy, reached over HTTPS with a certificate of the platform CA.
    litellm_url: str = Field(pattern=r"^https://[^/\s]+$")
    master_key_file: Path
    models: tuple[RouteModelSettings, ...] = Field(min_length=1)
    # How long a settlement waits for the proxy to record the last requests' spending.
    settle_wait_seconds: float = Field(default=25.0, ge=0, le=600)
    # The pinned OpenShell command-line tool, which manages the sessions' providers.
    openshell_cli: Path

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if Decimal(self.session_reservation) == 0:
            raise ValueError("session_reservation must be positive")
        return self


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
    actor_key_file: Path
    openshell_repository: str = ""


class GatewayTlsSettings(_Settings):
    """How the control service signs the serving certificates of the session gateways."""

    # The issuing CA that the init container writes, as tls.crt and tls.key.
    issuer_directory: Path
    # The headless Service of the gateway pods in the gateway namespace.
    service: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class Settings(_Settings):
    """The configuration file of the control service."""

    platform_id: Reference
    database: Path
    audience: str
    service_callers: frozenset[str]
    # Service accounts of platform operators, which may rotate project client secrets.
    operator_callers: frozenset[str] = frozenset()
    # Service accounts of platform services that read researchers' launch contexts.
    context_callers: frozenset[str] = frozenset()
    # Service accounts that reserve and settle the compute budgets of batch tasks: the
    # job service.
    job_callers: frozenset[str] = frozenset()
    # Contains ``{researcher}`` and ``{project}``; the researcher is a researcher identifier.
    redirect_uri_template: str
    # The key of the researcher identifiers in paths, Hub user names and redirect URIs.
    researcher_id_key_file: Path
    keycloak: KeycloakSettings
    # The serving certificate from the platform CA; the service serves only HTTPS.
    tls: TlsSettings
    # The files of each session's hosted gateway.
    hosted: HostedGatewaySettings
    sessions: SessionNamespaceSettings
    gateway_tls: GatewayTlsSettings
    projects: tuple[ProjectSettings, ...]
    synthetic_prices: PriceSheet
    inference: InferenceSettings
    # How project data volumes are filled; without it, sessions get none.
    project_data: ProjectDataSettings | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        identifiers = [project.id for project in self.projects]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("project identifiers must be unique")
        names = [model.name for model in self.inference.models]
        if len(set(names)) != len(names):
            raise ValueError("model names must be unique")
        if self.hosted.model_route.model not in names:
            raise ValueError("the worker's model is not a model of the route")
        if self.project_data is not None and self.project_data.workspace != WORKER_WORKSPACE:
            raise ValueError(
                f"project data volumes are attached to the workspace {WORKER_WORKSPACE}"
            )
        try:
            headroom = request_headroom(route_models(self), self.synthetic_prices)
        except InferenceSetupError as error:
            raise ValueError(str(error)) from None
        if Decimal(self.inference.session_reservation) <= headroom:
            raise ValueError(
                "session_reservation must exceed the largest cost of one request, "
                f"{headroom} for the route's models"
            )
        try:
            self.redirect_uri_template.format(researcher="researcher", project="project")
        except (IndexError, KeyError) as error:
            raise ValueError("redirect_uri_template has an unknown field") from error
        return self


def route_models(settings: Settings) -> tuple[RouteModel, ...]:
    """Return the models of the route."""
    return tuple(
        RouteModel(
            model.name, model.max_input_tokens, model.max_output_tokens, model.supports_tool_calls
        )
        for model in settings.inference.models
    )


def _inference(settings: Settings, ca_file: Path) -> ManagedInference:
    route = settings.hosted.model_route
    endpoint = urlsplit(route.endpoint)
    assert endpoint.hostname is not None
    master_key = settings.inference.master_key_file
    proxy = LiteLLM(
        httpx.Client(
            base_url=settings.inference.litellm_url,
            timeout=30,
            verify=ssl.create_default_context(cafile=ca_file),
        ),
        lambda: master_key.read_text(encoding="utf-8").strip(),
    )
    providers = OpenShellProviders(
        settings.inference.openshell_cli,
        ProviderRoute(
            name=route.provider,
            workspace=WORKER_WORKSPACE,
            placeholder_env=route.placeholder_env,
            host=endpoint.hostname,
            port=endpoint.port or 443,
            path=endpoint.path,
            binaries=route.binaries,
        ),
        Path(tempfile.gettempdir()),
    )
    return ManagedInference(
        settings.database,
        proxy,
        providers,
        route_models(settings),
        settings.synthetic_prices,
        settle_wait_seconds=settings.inference.settle_wait_seconds,
    )


def build(settings: Settings, kubernetes: Kubernetes) -> Starlette:
    """Assemble the application from its configuration."""
    ledger = Ledger(settings.database, settings.synthetic_prices.denomination)
    registry = Registry(settings.database)
    registry.synchronize(
        Project(project.id, project.billing_attribution_ref, project.policy_ref, project.members)
        for project in settings.projects
    )
    ledger.synchronize(
        Allocation(project.id, category, Decimal(amount))
        for project in settings.projects
        for category, amount in project.budgets.items()
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
            actor_key=read_key(namespaces.actor_key_file),
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
        models=route_models(settings),
    )
    project_data = (
        None
        if settings.project_data is None
        else ProjectData(
            kubernetes,
            settings.project_data,
            # The data service serves HTTPS with a certificate of the platform CA.
            httpx.Client(
                timeout=30,
                verify=ssl.create_default_context(cafile=namespaces.platform_ca_file),
            ),
        )
    )
    researcher_key = read_key(settings.researcher_id_key_file).encode()
    issuer = settings.keycloak.issuer
    launches = LaunchService(
        registry,
        keycloak,
        sessions,
        settings.platform_id,
        settings.redirect_uri_template,
        lambda subject: researcher_id(issuer, subject, researcher_key),
        hosted=hosted,
        ledger=ledger,
        session_reservation=Decimal(settings.inference.session_reservation),
        inference=_inference(settings, settings.sessions.platform_ca_file),
        project_data=project_data,
        certificates=GatewayCertificates(
            settings.gateway_tls.issuer_directory,
            settings.gateway_tls.service,
            namespaces.gateway_namespace,
        ),
    )
    launches.reconcile_dataset_grants()
    return create_app(
        launches,
        ResearcherTokens(settings.keycloak.issuer, settings.audience, keys),
        ServiceTokens(kubernetes.review_token, settings.audience, settings.service_callers),
        settings.synthetic_prices,
        ledger,
        operators=ServiceTokens(
            kubernetes.review_token, settings.audience, settings.operator_callers
        )
        if settings.operator_callers
        else None,
        contexts=ServiceTokens(kubernetes.review_token, settings.audience, settings.context_callers)
        if settings.context_callers
        else None,
        jobs=ServiceTokens(kubernetes.review_token, settings.audience, settings.job_callers)
        if settings.job_callers
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
