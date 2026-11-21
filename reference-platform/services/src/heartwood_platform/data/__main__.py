# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Entry point of the data service: python -m heartwood_platform.data CONFIGURATION."""

from __future__ import annotations

import logging
import ssl
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from heartwood_platform.common.kubernetes import Kubernetes, in_cluster_client
from heartwood_platform.common.serving import TlsSettings, serve
from heartwood_platform.common.tokens import ResearcherTokens, ServiceTokens
from heartwood_platform.data.access import ObjectAccess
from heartwood_platform.data.app import create_app
from heartwood_platform.data.catalog import Catalog
from heartwood_platform.data.signing import Credentials


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KeycloakSettings(_Settings):
    """Where the data service reads the keys that sign researchers' tokens."""

    url: str = Field(pattern=r"^https://[^/]+$")
    # The platform CA, the only authority the data service trusts.
    ca_file: Path
    issuer: str
    realm: str


class ObjectStoreSettings(_Settings):
    """The object store and the data service's read-only account."""

    endpoint: str = Field(pattern=r"^https://[^/]+$")
    region: str
    access_key_file: Path
    secret_key_file: Path


class Settings(_Settings):
    """The configuration file of the data service."""

    platform_id: str
    # The host name, with port, under which gateways reach the service; DRS URIs name it.
    hostname: str = Field(pattern=r"^[a-z0-9.-]+(:[0-9]{1,5})?$")
    audience: str
    # Service accounts that may ask for the access plan of a project data volume.
    service_callers: frozenset[str]
    keycloak: KeycloakSettings
    object_store: ObjectStoreSettings
    tls: TlsSettings
    catalog: Catalog


def build(
    settings: Settings,
    kubernetes: Kubernetes,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Starlette:
    """Assemble the application from its configuration."""
    client = httpx.Client(
        base_url=settings.keycloak.url,
        timeout=30,
        verify=ssl.create_default_context(cafile=settings.keycloak.ca_file),
    )

    def keys() -> dict[str, Any]:
        response = client.get(f"/realms/{settings.keycloak.realm}/protocol/openid-connect/certs")
        response.raise_for_status()
        document = response.json()
        assert isinstance(document, dict)
        return document

    store = settings.object_store

    def credentials() -> Credentials:
        # Read for each URL, so a replaced key takes effect without a restart.
        return Credentials(
            store.access_key_file.read_text(encoding="utf-8").strip(),
            store.secret_key_file.read_text(encoding="utf-8").strip(),
        )

    return create_app(
        settings.catalog,
        ResearcherTokens(settings.keycloak.issuer, settings.audience, keys),
        ServiceTokens(kubernetes.review_token, settings.audience, settings.service_callers),
        ObjectAccess(store.endpoint, store.region, credentials, clock),
        hostname=settings.hostname,
        platform_id=settings.platform_id,
    )


def main() -> None:
    """Serve the data service with the configuration file named on the command line."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = Settings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    application = build(settings, Kubernetes(in_cluster_client()))
    serve(application, settings.tls)


if __name__ == "__main__":
    main()
