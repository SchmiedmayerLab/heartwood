# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session namespaces: one namespace for each researcher and project.

The namespace holds the OpenShell gateway of the session and its sandboxes. The
control service creates it at the first launch, applies the same objects again at each
later launch, and deletes it with the session. Every write is confined by the
admission policy heartwood-control-scope.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from heartwood_platform.common.kubernetes import Kubernetes

NAMESPACE_PLACEHOLDER = "hw-s-placeholder"
GATEWAY_IMAGE_PLACEHOLDER = "heartwood.invalid/openshell-gateway:placeholder"
# The repository of the OpenShell release images that the sandbox gateway gives its pods.
OPENSHELL_REPOSITORY = "ghcr.io/nvidia/openshell"
_SANDBOX_IMAGES = ("supervisor", "sandbox")

_SESSION_LABEL = "heartwood.platform/session"
_RBAC = "rbac.authorization.k8s.io"
_NODE_READER = "heartwood-openshell-node-reader"
_OPENSHELL = {"app.kubernetes.io/name": "openshell", "app.kubernetes.io/instance": "openshell"}


class SessionError(Exception):
    """A session namespace could not be brought to its expected state."""


@dataclass(frozen=True)
class ApiServerAddress:
    """An address under which pods reach the Kubernetes API server."""

    cidr: str
    port: int


@dataclass(frozen=True)
class SessionSettings:
    """What the control service needs to provision session namespaces."""

    control_namespace: str
    control_account: str
    gateway_namespace: str
    openshell_objects: str
    openshell_gateway_image: str
    api_server: tuple[ApiServerAddress, ...]
    quota: dict[str, str]
    container_defaults: dict[str, str]
    container_default_requests: dict[str, str]
    # The platform CA certificate in PEM form, which sandbox supervisors trust.
    platform_ca: str
    # The deployment's actor key, which each hosted gateway receives as ``actor-key``.
    actor_key: str = field(repr=False)
    # The repository from which session pods pull the supervisor and sandbox runtime
    # images, each under its upstream name and digest. Empty keeps OPENSHELL_REPOSITORY.
    openshell_repository: str = ""
    ready_timeout_seconds: float = 180.0


@dataclass(frozen=True)
class ProvisionedSession:
    """A session namespace whose OpenShell gateway is ready."""

    namespace: str
    namespace_uid: str
    kube_system_uid: str
    gateway_account: str
    openshell_endpoint: str
    openshell_client: dict[str, str]


def session_namespace(session: str) -> str:
    """Return the namespace of a session."""
    return f"hw-s-{session}"


def gateway_account(session: str) -> str:
    """Return the name of the ServiceAccount and Secret of a session's Heartwood gateway."""
    return f"hw-gw-{session}"


def _subject(name: str, namespace: str) -> dict[str, str]:
    return {"kind": "ServiceAccount", "name": name, "namespace": namespace}


def _role_binding(namespace: str, role: str, subject: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": f"{_RBAC}/v1",
        "kind": "RoleBinding",
        "metadata": {"name": role, "namespace": namespace},
        "roleRef": {"apiGroup": _RBAC, "kind": "ClusterRole", "name": role},
        "subjects": [subject],
    }


def _policy(namespace: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


class Sessions:
    """Creates, reapplies and deletes session namespaces."""

    def __init__(
        self,
        kubernetes: Kubernetes,
        settings: SessionSettings,
        active_sessions: Callable[[], Iterable[str]],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Provision with the service's own Kubernetes rights.

        active_sessions lists every session, so the subjects of the node reader
        binding always follow the registry.
        """
        self._kubernetes = kubernetes
        self._settings = settings
        self._active_sessions = active_sessions
        self._sleep = sleep
        self._clock = clock
        self._kube_system_uid: str | None = None
        self._node_reader_lock = threading.Lock()
        # Sessions whose removal has begun. The registry lists a session until its
        # removal is complete, and a concurrent launch must not add its subject again.
        self._removed: set[str] = set()

    def _namespace_objects(self, session: str) -> list[dict[str, Any]]:
        """Quota, limits, network policies and platform objects of a session namespace."""
        namespace = session_namespace(session)
        settings = self._settings
        dns = {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
        supervisors = {"podSelector": {"matchLabels": {"openshell.ai/boundary-role": "supervisor"}}}
        gateway = {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": settings.gateway_namespace}
            },
            "podSelector": {"matchLabels": {_SESSION_LABEL: session}},
        }
        control = {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": settings.control_namespace}
            },
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "heartwood-platform",
                    "app.kubernetes.io/component": "control",
                }
            },
        }
        return [
            # The ServiceAccount of the staging pods that the Heartwood gateway creates.
            # It grants nothing, and staging pods mount no token.
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "hw-staging", "namespace": namespace},
                "automountServiceAccountToken": False,
            },
            # The OpenShell gateway mounts the platform CA certificate and passes it to
            # the supervisors of its sandboxes, which trust it for allowed destinations.
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "platform-ca", "namespace": namespace},
                "data": {"ca.crt": settings.platform_ca},
            },
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": "session", "namespace": namespace},
                "spec": {"hard": settings.quota},
            },
            {
                "apiVersion": "v1",
                "kind": "LimitRange",
                "metadata": {"name": "session", "namespace": namespace},
                "spec": {
                    "limits": [
                        {
                            "type": "Container",
                            "default": settings.container_defaults,
                            "defaultRequest": settings.container_default_requests,
                        }
                    ]
                },
            },
            _policy(
                namespace,
                "hw-default-deny",
                {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
            ),
            # The OpenShell gateway serves the supervisors of its sandboxes, the
            # Heartwood gateway of this session and the control service, which keeps the
            # provider of the model route.
            _policy(
                namespace,
                "hw-openshell-gateway-ingress",
                {
                    "podSelector": {"matchLabels": _OPENSHELL},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [supervisors, gateway, control],
                            "ports": [{"protocol": "TCP", "port": 8080}],
                        }
                    ],
                },
            ),
            # The OpenShell gateway and its certificate Job call the Kubernetes API.
            _policy(
                namespace,
                "hw-openshell-api-egress",
                {
                    "podSelector": {"matchLabels": {"app.kubernetes.io/instance": "openshell"}},
                    "policyTypes": ["Egress"],
                    "egress": [
                        {
                            "to": [dns],
                            "ports": [
                                {"protocol": "UDP", "port": 53},
                                {"protocol": "TCP", "port": 53},
                            ],
                        },
                        *(
                            {
                                "to": [{"ipBlock": {"cidr": address.cidr}}],
                                "ports": [{"protocol": "TCP", "port": address.port}],
                            }
                            for address in settings.api_server
                        ),
                    ],
                },
            ),
        ]

    def _openshell_objects(self, namespace: str) -> list[dict[str, Any]]:
        text = self._settings.openshell_objects.replace(NAMESPACE_PLACEHOLDER, namespace).replace(
            GATEWAY_IMAGE_PLACEHOLDER, self._settings.openshell_gateway_image
        )
        if self._settings.openshell_repository:
            # Only the repository changes; each reference keeps the digest of the release.
            for name in _SANDBOX_IMAGES:
                text = text.replace(
                    f"{OPENSHELL_REPOSITORY}/{name}@",
                    f"{self._settings.openshell_repository}/{name}@",
                )
        items = json.loads(text)["items"]
        assert isinstance(items, list)
        return items

    def _apply_node_reader(self, session: str, *, removed: bool = False) -> None:
        """Set the subjects of the node reader binding to the sessions of the registry.

        A launch of a session whose removal failed midway provisions it again, so it
        counts as removed only until its next launch.
        """
        with self._node_reader_lock:
            if removed:
                self._removed.add(session)
            else:
                self._removed.discard(session)
            sessions = sorted(set(self._active_sessions()) - self._removed)
            self._kubernetes.apply(
                {
                    "apiVersion": f"{_RBAC}/v1",
                    "kind": "ClusterRoleBinding",
                    "metadata": {"name": _NODE_READER},
                    "subjects": [
                        _subject("openshell", session_namespace(session)) for session in sessions
                    ],
                }
            )

    def _cluster_uid(self) -> str:
        if self._kube_system_uid is None:
            namespace = self._kubernetes.get("Namespace", "kube-system")
            if namespace is None:
                raise SessionError("the kube-system namespace is not readable")
            self._kube_system_uid = namespace["metadata"]["uid"]
        return self._kube_system_uid

    def _wait(self, describe: str, deadline: float, ready: Callable[[], Any]) -> Any:  # noqa: ANN401
        while True:
            result = ready()
            if result:
                return result
            if self._clock() >= deadline:
                raise SessionError(f"{describe} is not ready")
            self._sleep(2.0)

    def provision(self, session: str) -> ProvisionedSession:
        """Bring the namespace of a session to its full state and wait for its gateway."""
        settings = self._settings
        kubernetes = self._kubernetes
        namespace = session_namespace(session)
        labels = {
            _SESSION_LABEL: session,
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "latest",
        }
        kubernetes.create(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace, "labels": labels},
            }
        )
        control = _subject(settings.control_account, settings.control_namespace)
        kubernetes.create(_role_binding(namespace, "heartwood-session-control", control))
        # The binding lets the service read the namespace. An existing namespace is
        # reused only when it carries the labels that a created one would.
        existing = kubernetes.get("Namespace", namespace)
        if existing is None or existing["metadata"].get("deletionTimestamp") is not None:
            raise SessionError("the session namespace is being deleted")
        found = existing["metadata"].get("labels", {})
        if any(found.get(key) != value for key, value in labels.items()):
            raise SessionError("an existing namespace does not carry the session labels")

        for document in self._namespace_objects(session):
            kubernetes.apply(document)
        # One key for the lifetime of the session: OpenShell encrypts the provider
        # credentials of the session with it.
        key = base64.b64encode(secrets.token_bytes(32))
        kubernetes.create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "openshell-kek", "namespace": namespace},
                "type": "Opaque",
                "data": {"key-encryption-key": base64.b64encode(key).decode()},
            }
        )
        openshell = _subject("openshell", namespace)
        account = gateway_account(session)
        for role, subject in (
            ("heartwood-openshell-sandbox", openshell),
            ("heartwood-openshell-peer", openshell),
            ("heartwood-openshell-certgen", _subject("openshell-certgen", namespace)),
            ("heartwood-session-gateway", _subject(account, settings.gateway_namespace)),
        ):
            kubernetes.create(_role_binding(namespace, role, subject))
        for document in self._openshell_objects(namespace):
            kubernetes.apply(document)
        self._apply_node_reader(session)
        kubernetes.create(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": account, "namespace": settings.gateway_namespace},
                "automountServiceAccountToken": False,
            }
        )

        deadline = self._clock() + settings.ready_timeout_seconds
        client = self._wait(
            "the OpenShell client certificate",
            deadline,
            lambda: kubernetes.get("Secret", "openshell-client-tls", namespace),
        )
        self._wait(
            "the OpenShell gateway",
            deadline,
            lambda: (
                (kubernetes.get("StatefulSet", "openshell", namespace) or {})
                .get("status", {})
                .get("readyReplicas")
            ),
        )
        return ProvisionedSession(
            namespace=namespace,
            namespace_uid=existing["metadata"]["uid"],
            kube_system_uid=self._cluster_uid(),
            gateway_account=account,
            openshell_endpoint=f"https://openshell.{namespace}.svc.cluster.local:8080",
            openshell_client={
                name: base64.b64decode(value).decode() for name, value in client["data"].items()
            },
        )

    def write_gateway_secret(self, session: str, files: dict[str, str]) -> None:
        """Replace the configuration Secret of a session's Heartwood gateway.

        The Secret also carries the platform CA certificate as ``platform-ca.crt``, the
        only authority the gateway trusts for Keycloak and the platform services, and
        the deployment's actor key as ``actor-key``.
        """
        files = {
            **files,
            "platform-ca.crt": self._settings.platform_ca,
            "actor-key": self._settings.actor_key,
        }
        namespace = self._settings.gateway_namespace
        name = gateway_account(session)
        # The service may create and delete Secrets there, and not read or update them.
        self._kubernetes.delete("Secret", name, namespace)
        created = self._kubernetes.create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {_SESSION_LABEL: session},
                },
                "type": "Opaque",
                "data": {
                    key: base64.b64encode(value.encode()).decode() for key, value in files.items()
                },
            }
        )
        if not created:
            raise SessionError("the gateway Secret was created by another party")

    def remove(self, session: str) -> None:
        """Delete everything of a session, whether or not it was fully provisioned."""
        namespace = self._settings.gateway_namespace
        self._apply_node_reader(session, removed=True)
        self._kubernetes.delete("Secret", gateway_account(session), namespace)
        self._kubernetes.delete("ServiceAccount", gateway_account(session), namespace)
        self._kubernetes.delete("Namespace", session_namespace(session))
