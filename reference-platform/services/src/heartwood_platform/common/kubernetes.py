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


# Path prefix, resource name and scope of the object kinds that the services write.
_RESOURCES = {
    "Namespace": ("/api/v1", "namespaces", False),
    "ConfigMap": ("/api/v1", "configmaps", True),
    "PersistentVolumeClaim": ("/api/v1", "persistentvolumeclaims", True),
    "LimitRange": ("/api/v1", "limitranges", True),
    "ResourceQuota": ("/api/v1", "resourcequotas", True),
    "Secret": ("/api/v1", "secrets", True),
    "Service": ("/api/v1", "services", True),
    "ServiceAccount": ("/api/v1", "serviceaccounts", True),
    "StatefulSet": ("/apis/apps/v1", "statefulsets", True),
    "Job": ("/apis/batch/v1", "jobs", True),
    "NetworkPolicy": ("/apis/networking.k8s.io/v1", "networkpolicies", True),
    "RoleBinding": ("/apis/rbac.authorization.k8s.io/v1", "rolebindings", True),
    "ClusterRoleBinding": ("/apis/rbac.authorization.k8s.io/v1", "clusterrolebindings", False),
}
_FIELD_MANAGER = "heartwood-control"


def _collection(kind: str, namespace: str | None) -> str:
    prefix, resource, namespaced = _RESOURCES[kind]
    if namespaced != (namespace is not None):
        raise ValueError(f"{kind} is {'' if namespaced else 'not '}namespaced")
    return f"{prefix}/namespaces/{namespace}/{resource}" if namespaced else f"{prefix}/{resource}"


class Kubernetes:
    """The Kubernetes API calls of the platform services."""

    def __init__(self, client: httpx.Client) -> None:
        """Use a client that is authenticated to the API server."""
        self._client = client

    def create(self, document: dict[str, Any]) -> bool:
        """Create an object. Return False when an object of that name exists."""
        metadata = document["metadata"]
        response = self._client.post(
            _collection(document["kind"], metadata.get("namespace")), json=document
        )
        if response.status_code == httpx.codes.CONFLICT:
            return False
        response.raise_for_status()
        return True

    def apply(self, document: dict[str, Any]) -> None:
        """Create or update an object with server-side apply, taking over its fields."""
        metadata = document["metadata"]
        collection = _collection(document["kind"], metadata.get("namespace"))
        self._client.patch(
            f"{collection}/{metadata['name']}",
            params={"fieldManager": _FIELD_MANAGER, "force": "true"},
            headers={"Content-Type": "application/apply-patch+yaml"},
            json=document,
        ).raise_for_status()

    def get(self, kind: str, name: str, namespace: str | None = None) -> dict[str, Any] | None:
        """Return an object, or None when it does not exist."""
        response = self._client.get(f"{_collection(kind, namespace)}/{name}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        document = response.json()
        assert isinstance(document, dict)
        return document

    def list(self, kind: str, namespace: str, selector: str) -> list[dict[str, Any]]:
        """Return the objects of a kind in a namespace that a label selector selects."""
        response = self._client.get(
            _collection(kind, namespace), params={"labelSelector": selector}
        )
        response.raise_for_status()
        items = response.json().get("items")
        if not isinstance(items, list):
            raise ValueError(f"the {kind} listing has no items")
        return items

    def delete(
        self, kind: str, name: str, namespace: str | None = None, *, propagation: str | None = None
    ) -> None:
        """Delete an object, whether or not it exists or is already being deleted.

        The API server answers a repeated deletion of a namespace that is still being
        emptied with a conflict. ``propagation`` sets the deletion's propagation policy:
        with ``Foreground``, the object stays until its dependents, such as a Job's pods,
        are deleted.
        """
        response = self._client.delete(
            f"{_collection(kind, namespace)}/{name}",
            params={} if propagation is None else {"propagationPolicy": propagation},
        )
        if response.status_code not in {httpx.codes.NOT_FOUND, httpx.codes.CONFLICT}:
            response.raise_for_status()

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
