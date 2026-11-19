# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Access to the Kubernetes API with the service account of the pod."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx

_SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class _ServiceAccountToken(httpx.Auth):
    """Reads the token for each request, because the kubelet rotates the file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = (
            f"Bearer {self._path.read_text(encoding='utf-8').strip()}"
        )
        yield request


def in_cluster_client(directory: Path = _SERVICE_ACCOUNT) -> httpx.Client:
    """Return a client for the API server that authenticates as the pod."""
    return httpx.Client(
        base_url="https://kubernetes.default.svc",
        verify=str(directory / "ca.crt"),
        auth=_ServiceAccountToken(directory / "token"),
        timeout=30,
    )


class Kubernetes:
    """The Kubernetes API calls of the platform services."""

    def __init__(self, client: httpx.Client) -> None:
        """Use a client that is authenticated to the API server."""
        self._client = client

    def review_token(self, token: str, audience: str) -> dict[str, Any]:
        """Return the status of a TokenReview of a token for one audience."""
        response = self._client.post(
            "/apis/authentication.k8s.io/v1/tokenreviews",
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": token, "audiences": [audience]},
            },
        )
        response.raise_for_status()
        status = response.json().get("status", {})
        return status if isinstance(status, dict) else {}
