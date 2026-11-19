# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Entry point of the control service: python -m heartwood_platform.control CONFIGURATION."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from pydantic import BaseModel, ConfigDict
from starlette.applications import Starlette

from heartwood_platform.common.kubernetes import Kubernetes, in_cluster_client
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.control.app import ProjectId, Subject, create_app
from heartwood_platform.control.keycloak import Keycloak
from heartwood_platform.control.launch import LaunchService
from heartwood_platform.control.registry import Project, Registry


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KeycloakSettings(_Settings):
    """How the control service reaches Keycloak."""

    url: str
    issuer: str
    realm: str
    client_id: str
    client_secret_file: Path


class ProjectSettings(_Settings):
    """One project of the registry."""

    id: ProjectId
    billing_attribution_ref: str
    policy_ref: str
    members: frozenset[Subject] = frozenset()


class Settings(_Settings):
    """The configuration file of the control service."""

    platform_id: str
    database: Path
    audience: str
    service_callers: frozenset[str]
    redirect_uri_template: str
    keycloak: KeycloakSettings
    projects: tuple[ProjectSettings, ...]


def build(settings: Settings, kubernetes: Kubernetes) -> Starlette:
    """Assemble the application from its configuration."""
    registry = Registry(settings.database)
    registry.synchronize(
        Project(project.id, project.billing_attribution_ref, project.policy_ref, project.members)
        for project in settings.projects
    )
    client = httpx.Client(base_url=settings.keycloak.url, timeout=30)
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

    return create_app(
        LaunchService(registry, keycloak, settings.platform_id, settings.redirect_uri_template),
        ResearcherTokens(settings.keycloak.issuer, settings.audience, keys),
        ServiceTokens(kubernetes.review_token, settings.audience, settings.service_callers),
    )


def main() -> None:
    """Serve the control service with the configuration file named on the command line."""
    settings = Settings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    application = build(settings, Kubernetes(in_cluster_client()))
    uvicorn.run(application, host="0.0.0.0", port=8080, log_config=None)


if __name__ == "__main__":
    main()
