# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Management of project clients through the Keycloak admin API.

The control service authenticates as its own client, whose service account holds the
role manage-clients, and requests the scope kc-admin-api. It creates one confidential
client for each project and maintains that client's redirect URIs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx

SERVICE_SCOPES = ("svc-control", "svc-data", "svc-jobs")
# Seconds before its expiry at which an administration token is replaced.
_TOKEN_MARGIN = 30.0


def project_client(project: str) -> str:
    """Return the client identifier of a project."""
    return f"hw-proj-{project}"


class Keycloak:
    """Project client management in one realm."""

    def __init__(
        self,
        client: httpx.Client,
        realm: str,
        client_id: str,
        client_secret: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Use an HTTP client whose base URL is the Keycloak server."""
        self._client = client
        self._realm = realm
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._token: tuple[str, float] | None = None
        self._token_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _authorization(self) -> dict[str, str]:
        with self._token_lock:
            if self._token is None or self._clock() >= self._token[1]:
                response = self._client.post(
                    f"/realms/{self._realm}/protocol/openid-connect/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret(),
                        "scope": "kc-admin-api",
                    },
                )
                response.raise_for_status()
                body = response.json()
                expiry = self._clock() + float(body["expires_in"]) - _TOKEN_MARGIN
                self._token = (body["access_token"], expiry)
            return {"Authorization": f"Bearer {self._token[0]}"}

    def _request(self, method: str, path: str, **arguments: Any) -> httpx.Response:  # noqa: ANN401
        return self._client.request(
            method,
            f"/admin/realms/{self._realm}{path}",
            headers=self._authorization(),
            **arguments,
        )

    @contextmanager
    def _serialized(self, client_id: str) -> Iterator[None]:
        """Serialize changes to one client.

        The admin API replaces the whole client representation on an update, so two
        concurrent read-modify-write sequences would lose one change.
        """
        with self._locks_guard:
            lock = self._locks.setdefault(client_id, threading.Lock())
        with lock:
            yield

    def _find(self, client_id: str) -> dict[str, Any] | None:
        response = self._request("GET", "/clients", params={"clientId": client_id})
        response.raise_for_status()
        for client in response.json():
            if client.get("clientId") == client_id:
                assert isinstance(client, dict)
                return client
        return None

    def _existing(self, client_id: str) -> dict[str, Any]:
        client = self._find(client_id)
        if client is None:
            raise LookupError(client_id)
        return client

    def ensure_project_client(self, project: str) -> None:
        """Create the client of a project unless it exists, and attach the service scopes."""
        client_id = project_client(project)
        with self._serialized(client_id):
            client = self._find(client_id)
            if client is None:
                response = self._request(
                    "POST",
                    "/clients",
                    json={
                        "clientId": client_id,
                        "protocol": "openid-connect",
                        "publicClient": False,
                        "standardFlowEnabled": True,
                        "implicitFlowEnabled": False,
                        "directAccessGrantsEnabled": False,
                        "serviceAccountsEnabled": False,
                        "redirectUris": [],
                        "attributes": {"standard.token.exchange.enabled": "true"},
                    },
                )
                # An administrator may have created it since the lookup.
                if response.status_code != httpx.codes.CONFLICT:
                    response.raise_for_status()
                client = self._existing(client_id)
            missing = set(SERVICE_SCOPES) - set(client.get("optionalClientScopes", []))
            if not missing:
                return
            response = self._request("GET", "/client-scopes")
            response.raise_for_status()
            scopes = {scope["name"]: scope["id"] for scope in response.json()}
            for name in sorted(missing):
                self._request(
                    "PUT", f"/clients/{client['id']}/optional-client-scopes/{scopes[name]}"
                ).raise_for_status()

    def _change_redirect_uris(self, project: str, uri: str, *, present: bool) -> None:
        client_id = project_client(project)
        with self._serialized(client_id):
            client = self._find(client_id)
            if client is None:
                if present:
                    raise LookupError(client_id)
                return
            uris = set(client.get("redirectUris", []))
            changed = uris | {uri} if present else uris - {uri}
            if changed == uris:
                return
            client["redirectUris"] = sorted(changed)
            self._request("PUT", f"/clients/{client['id']}", json=client).raise_for_status()

    def add_redirect_uri(self, project: str, uri: str) -> None:
        """Register a redirect URI with the client of a project."""
        self._change_redirect_uris(project, uri, present=True)

    def remove_redirect_uri(self, project: str, uri: str) -> None:
        """Remove a redirect URI from the client of a project."""
        self._change_redirect_uris(project, uri, present=False)

    def client_secret(self, project: str) -> str:
        """Return the secret of the client of a project."""
        client = self._existing(project_client(project))
        response = self._request("GET", f"/clients/{client['id']}/client-secret")
        response.raise_for_status()
        secret = response.json()["value"]
        assert isinstance(secret, str)
        return secret
