# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Positive and negative cases of both admission policies on an installed chart.

Requests are sent with server-side dry run as the confined ServiceAccount, so they pass
authorization and admission and change nothing. Exec, attach and port-forward requests
have no dry run and act on probe pods.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest
from kube import CONTROL, Cluster

_SESSION = "policytest"
_NAMESPACE = f"hw-s-{_SESSION}"
# A namespace without the restricted Pod Security label, so that a denial of a pod
# comes from the staging policy alone.
_OPEN_NAMESPACE = "hw-s-policyopen"
_GATEWAY = f"system:serviceaccount:heartwood-sessions:hw-gw-{_SESSION}"

_CONTROL_SCOPE = "ValidatingAdmissionPolicy 'heartwood-control-scope'"
_STAGING_SHAPE = "ValidatingAdmissionPolicy 'heartwood-staging-shape'"

_OUTSIDE = "writes only in session namespaces"
_NAMESPACE_NAME = "only namespaces named hw-s-<session>"
_NAMESPACE_LABELS = "session label and the restricted Pod Security label"
_PAIRING = "pairs a fixed ClusterRole with its only allowed subject"
_NODE_READER = "adds and removes only the OpenShell ServiceAccounts"
_GATEWAY_OBJECTS = "writes only objects named hw-gw-<session>"
_SECRET_TYPE = "writes only Secrets of the type Opaque"


def _namespace(name: str, labels: dict[str, str]) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}}


def _session_labels(session: str) -> dict[str, str]:
    return {
        "heartwood.platform/session": session,
        "pod-security.kubernetes.io/enforce": "restricted",
    }


def _account(name: str, namespace: str) -> dict[str, str]:
    return {"kind": "ServiceAccount", "name": name, "namespace": namespace}


def _binding(
    role: str, *subjects: dict[str, str], namespace: str = _NAMESPACE, name: str = "case"
) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": list(subjects),
    }


def _role_binding(role: str, subject: dict[str, str]) -> dict[str, Any]:
    """Bind a namespaced Role, which the session fixture creates without rules."""
    binding = _binding(role, subject)
    binding["roleRef"]["kind"] = "Role"
    return binding


def _node_reader(*subjects: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": "heartwood-openshell-node-reader"},
        "subjects": list(subjects),
    }


def _named(kind: str, name: str, namespace: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": namespace}}


def _pod(
    image: str, name: str = "hw-staging-case", namespace: str = _OPEN_NAMESPACE
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "serviceAccountName": "hw-staging",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "staging",
                    "image": image,
                    "args": ["pause"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}},
                }
            ],
            "volumes": [
                {"name": "inputs", "persistentVolumeClaim": {"claimName": "hw-attempt-a-inputs"}},
                {"name": "scratch", "emptyDir": {}},
            ],
        },
    }


def _claim(name: str = "hw-attempt-a-inputs") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": _NAMESPACE},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "heartwood-attempt",
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }


def _changed(document: dict[str, Any], path: str, value: object = None) -> dict[str, Any]:
    """Copy a document and set the dotted path, or remove it when no value is given."""
    changed = copy.deepcopy(document)
    target: Any = changed
    *parents, last = path.split(".")
    for parent in parents:
        target = target[int(parent)] if isinstance(target, list) else target[parent]
    key = int(last) if isinstance(target, list) else last
    if value is None:
        del target[key]
    else:
        target[key] = value
    return changed


# An existing RuntimeClass, so that naming it reaches the staging policy.
_RUNTIME_CLASS: dict[str, Any] = {
    "apiVersion": "node.k8s.io/v1",
    "kind": "RuntimeClass",
    "metadata": {"name": "hw-policy-test"},
    "handler": "runc",
}


@pytest.fixture(scope="module", autouse=True)
def session(cluster: Cluster, staging_image: str) -> Iterator[None]:
    """Create the objects that the cases refer to, as the cluster administrator."""
    probe = _pod(staging_image, name="hw-staging-probe", namespace=_NAMESPACE)
    probe["spec"]["volumes"] = []
    other = copy.deepcopy(probe)
    other["metadata"]["name"] = "other-probe"
    gateway = _account(f"hw-gw-{_SESSION}", "heartwood-sessions")
    wider = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "wider-than-the-gateway-role", "namespace": _NAMESPACE},
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["pods/attach", "pods/portforward", "pods", "persistentvolumeclaims"],
                "verbs": ["create", "get", "patch", "update"],
            }
        ],
    }
    wider_binding = _binding("wider-than-the-gateway-role", gateway, name="wider")
    wider_binding["roleRef"]["kind"] = "Role"
    # A Role without rules, which authorization lets anyone bind.
    empty = {**wider, "metadata": {"name": "heartwood-openshell-sandbox", "namespace": _NAMESPACE}}
    empty["rules"] = []
    cluster.apply(
        _namespace(_NAMESPACE, _session_labels(_SESSION)),
        _namespace(_OPEN_NAMESPACE, {"heartwood.platform/session": "policyopen"}),
    )
    cluster.apply(
        _named("ServiceAccount", "hw-staging", _NAMESPACE),
        _named("ServiceAccount", "hw-staging", _OPEN_NAMESPACE),
        _named("ServiceAccount", f"hw-gw-{_SESSION}", "heartwood-sessions"),
        _named("Secret", f"hw-gw-{_SESSION}", "heartwood-sessions"),
        _named("Secret", "foreign", "heartwood-sessions"),
        _binding("heartwood-session-gateway", gateway, name="heartwood-session-gateway"),
        _binding(
            "heartwood-session-gateway",
            gateway,
            namespace=_OPEN_NAMESPACE,
            name="heartwood-session-gateway",
        ),
        _binding("view", _account("default", _NAMESPACE), name="foreign"),
        _binding(
            "heartwood-session-control",
            _account("heartwood-control", "heartwood-platform"),
            name="heartwood-session-control",
        ),
        wider,
        wider_binding,
        empty,
        _RUNTIME_CLASS,
        probe,
        other,
    )
    ready = cluster.kubectl(
        "wait", "--namespace", _NAMESPACE, "--for=condition=Ready", "pod", "--all", "--timeout=180s"
    )
    assert ready.returncode == 0, ready.stderr
    yield
    cluster.kubectl("delete", "runtimeclass", _RUNTIME_CLASS["metadata"]["name"])
    cluster.kubectl("delete", "namespace", _NAMESPACE, _OPEN_NAMESPACE, "--wait")
    cluster.kubectl(
        "delete", "--namespace", "heartwood-sessions", "--ignore-not-found", "secret", "foreign"
    )
    cluster.kubectl(
        "delete",
        "--namespace",
        "heartwood-sessions",
        "--ignore-not-found",
        f"serviceaccount/hw-gw-{_SESSION}",
        f"secret/hw-gw-{_SESSION}",
    )


def _request(cluster: Cluster, actor: str, verb: str, document: dict[str, Any]) -> tuple[bool, str]:
    """Send one dry-run request and report whether it was admitted."""
    if verb == "create":
        arguments = ["create", "--validate=false", "--filename", "-"]
    elif verb == "apply":
        arguments = ["apply", "--server-side", "--force-conflicts", "--validate=false"]
        arguments += ["--filename", "-"]
    else:
        arguments = ["delete", "--filename", "-"]
    result = cluster.kubectl(*arguments, "--dry-run=server", actor=actor, document=document)
    return result.returncode == 0, result.stderr


def _expect(
    cluster: Cluster,
    actor: str,
    verb: str,
    document: dict[str, Any],
    policy: str,
    message: str | None,
) -> None:
    admitted, error = _request(cluster, actor, verb, document)
    if message is None:
        assert admitted, error
    else:
        assert not admitted
        assert policy in error, error
        assert message in error, error


_OPENSHELL = _account("openshell", _NAMESPACE)
_CERTGEN = _account("openshell-certgen", _NAMESPACE)
_SESSION_GATEWAY = _account(f"hw-gw-{_SESSION}", "heartwood-sessions")
_CONTROL_ACCOUNT = _account("heartwood-control", "heartwood-platform")

_CONTROL_CASES: list[tuple[str, str, dict[str, Any], str | None]] = [
    # Namespaces.
    ("namespace", "create", _namespace("hw-s-new", _session_labels("new")), None),
    ("namespace-delete", "delete", _namespace(_NAMESPACE, {}), None),
    (
        "namespace-other-name",
        "create",
        _namespace("other", _session_labels("new")),
        _NAMESPACE_NAME,
    ),
    (
        "namespace-name-with-suffix",
        "create",
        _namespace("hw-s-new-x", _session_labels("new-x")),
        _NAMESPACE_NAME,
    ),
    (
        "namespace-delete-other",
        "delete",
        _namespace("heartwood-sessions", {}),
        _NAMESPACE_NAME,
    ),
    (
        "namespace-without-pod-security",
        "create",
        _namespace("hw-s-new", {"heartwood.platform/session": "new"}),
        _NAMESPACE_LABELS,
    ),
    (
        "namespace-baseline-pod-security",
        "create",
        _namespace(
            "hw-s-new",
            {"heartwood.platform/session": "new", "pod-security.kubernetes.io/enforce": "baseline"},
        ),
        _NAMESPACE_LABELS,
    ),
    (
        "namespace-latest-pod-security-version",
        "create",
        _namespace(
            "hw-s-new",
            {**_session_labels("new"), "pod-security.kubernetes.io/enforce-version": "latest"},
        ),
        None,
    ),
    (
        "namespace-old-pod-security-version",
        "create",
        _namespace(
            "hw-s-new",
            {**_session_labels("new"), "pod-security.kubernetes.io/enforce-version": "v1.0"},
        ),
        _NAMESPACE_LABELS,
    ),
    (
        "namespace-without-session-label",
        "create",
        _namespace("hw-s-new", {"pod-security.kubernetes.io/enforce": "restricted"}),
        _NAMESPACE_LABELS,
    ),
    (
        "namespace-other-session-label",
        "create",
        _namespace("hw-s-new", _session_labels("other")),
        _NAMESPACE_LABELS,
    ),
    # Each fixed ClusterRole with its only allowed subject.
    ("bind-sandbox", "create", _binding("heartwood-openshell-sandbox", _OPENSHELL), None),
    ("bind-peer", "create", _binding("heartwood-openshell-peer", _OPENSHELL), None),
    ("bind-certgen", "create", _binding("heartwood-openshell-certgen", _CERTGEN), None),
    ("bind-gateway", "create", _binding("heartwood-session-gateway", _SESSION_GATEWAY), None),
    ("bind-control", "create", _binding("heartwood-session-control", _CONTROL_ACCOUNT), None),
    (
        "unbind-gateway",
        "delete",
        _binding("heartwood-session-gateway", name="heartwood-session-gateway"),
        None,
    ),
    # Each fixed ClusterRole with another subject.
    (
        "bind-sandbox-to-certgen",
        "create",
        _binding("heartwood-openshell-sandbox", _CERTGEN),
        _PAIRING,
    ),
    (
        "bind-sandbox-to-another-session",
        "create",
        _binding("heartwood-openshell-sandbox", _account("openshell", "hw-s-other")),
        _PAIRING,
    ),
    (
        "bind-peer-to-gateway",
        "create",
        _binding("heartwood-openshell-peer", _SESSION_GATEWAY),
        _PAIRING,
    ),
    (
        "bind-peer-to-another-session",
        "create",
        _binding("heartwood-openshell-peer", _account("openshell", "hw-s-other")),
        _PAIRING,
    ),
    (
        "bind-certgen-to-openshell",
        "create",
        _binding("heartwood-openshell-certgen", _OPENSHELL),
        _PAIRING,
    ),
    (
        "bind-certgen-to-another-session",
        "create",
        _binding("heartwood-openshell-certgen", _account("openshell-certgen", "hw-s-other")),
        _PAIRING,
    ),
    (
        "bind-gateway-to-another-session",
        "create",
        _binding("heartwood-session-gateway", _account("hw-gw-other", "heartwood-sessions")),
        _PAIRING,
    ),
    (
        "bind-gateway-to-another-namespace",
        "create",
        _binding("heartwood-session-gateway", _account(f"hw-gw-{_SESSION}", _NAMESPACE)),
        _PAIRING,
    ),
    (
        "bind-gateway-to-control",
        "create",
        _binding("heartwood-session-gateway", _CONTROL_ACCOUNT),
        _PAIRING,
    ),
    (
        "bind-control-to-gateway",
        "create",
        _binding("heartwood-session-control", _SESSION_GATEWAY),
        _PAIRING,
    ),
    (
        "bind-control-to-another-namespace",
        "create",
        _binding("heartwood-session-control", _account("heartwood-control", "heartwood-sessions")),
        _PAIRING,
    ),
    (
        "bind-node-reader-in-a-namespace",
        "create",
        _binding("heartwood-openshell-node-reader", _OPENSHELL),
        _PAIRING,
    ),
    (
        "bind-to-a-user",
        "create",
        _binding(
            "heartwood-openshell-sandbox",
            {"kind": "User", "name": "openshell", "apiGroup": "rbac.authorization.k8s.io"},
        ),
        _PAIRING,
    ),
    (
        "bind-to-two-subjects",
        "create",
        _binding("heartwood-openshell-sandbox", _OPENSHELL, _CERTGEN),
        _PAIRING,
    ),
    ("unbind-foreign", "delete", _binding("view", name="foreign"), _PAIRING),
    # Roles that authorization lets the control service bind, because it holds their rules.
    (
        "bind-another-cluster-role",
        "create",
        _binding("system:public-info-viewer", _OPENSHELL),
        _PAIRING,
    ),
    ("bind-a-role", "create", _role_binding("heartwood-openshell-sandbox", _OPENSHELL), _PAIRING),
    # A valid pair outside a session namespace.
    (
        "bind-in-default",
        "create",
        _binding("heartwood-session-control", _CONTROL_ACCOUNT, namespace="default"),
        _OUTSIDE,
    ),
    (
        "bind-in-platform-namespace",
        "create",
        _binding("heartwood-session-control", _CONTROL_ACCOUNT, namespace="heartwood-platform"),
        _OUTSIDE,
    ),
    (
        "bind-in-sessions-namespace",
        "create",
        _binding("heartwood-session-control", _CONTROL_ACCOUNT, namespace="heartwood-sessions"),
        _OUTSIDE,
    ),
    # Subjects of the node reader binding.
    ("node-reader-add", "apply", _node_reader(_OPENSHELL), None),
    (
        "node-reader-add-other-account",
        "apply",
        _node_reader(_OPENSHELL, _account("default", _NAMESPACE)),
        _NODE_READER,
    ),
    (
        "node-reader-add-outside-sessions",
        "apply",
        _node_reader(_account("openshell", "default")),
        _NODE_READER,
    ),
    (
        "node-reader-add-user",
        "apply",
        _node_reader(
            {"kind": "User", "name": "openshell", "apiGroup": "rbac.authorization.k8s.io"}
        ),
        _NODE_READER,
    ),
    # Gateway objects in heartwood-sessions.
    (
        "gateway-account",
        "create",
        _named("ServiceAccount", "hw-gw-new", "heartwood-sessions"),
        None,
    ),
    ("gateway-secret", "create", _named("Secret", "hw-gw-new", "heartwood-sessions"), None),
    (
        "gateway-secret-delete",
        "delete",
        _named("Secret", f"hw-gw-{_SESSION}", "heartwood-sessions"),
        None,
    ),
    (
        "other-account",
        "create",
        _named("ServiceAccount", "hub", "heartwood-sessions"),
        _GATEWAY_OBJECTS,
    ),
    ("other-secret", "create", _named("Secret", "hub", "heartwood-sessions"), _GATEWAY_OBJECTS),
    (
        "gateway-token-secret",
        "create",
        {
            **_named("Secret", "hw-gw-new", "heartwood-sessions"),
            "type": "kubernetes.io/service-account-token",
            "metadata": {
                "name": "hw-gw-new",
                "namespace": "heartwood-sessions",
                "annotations": {"kubernetes.io/service-account.name": "default"},
            },
        },
        _SECRET_TYPE,
    ),
    (
        "other-secret-delete",
        "delete",
        _named("Secret", "foreign", "heartwood-sessions"),
        _GATEWAY_OBJECTS,
    ),
]


@pytest.mark.parametrize(
    ("verb", "document", "message"),
    [pytest.param(*case[1:], id=case[0]) for case in _CONTROL_CASES],
)
def test_control_scope(
    cluster: Cluster, verb: str, document: dict[str, Any], message: str | None
) -> None:
    _expect(cluster, CONTROL, verb, document, _CONTROL_SCOPE, message)


def test_control_scope_refuses_removing_a_foreign_node_reader_subject(cluster: Cluster) -> None:
    def administrator(document: dict[str, Any]) -> None:
        result = cluster.kubectl(
            "apply",
            "--server-side",
            "--field-manager=policy-test",
            # The control service owns the subjects once it has provisioned a session.
            "--force-conflicts",
            "--filename",
            "-",
            document=document,
        )
        assert result.returncode == 0, result.stderr

    administrator(_node_reader(_account("default", "default")))
    try:
        _expect(cluster, CONTROL, "apply", _node_reader(_OPENSHELL), _CONTROL_SCOPE, _NODE_READER)
    finally:
        # An apply without the field gives up its ownership and removes the subject.
        released = _node_reader()
        del released["subjects"]
        administrator(released)


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "ownerReferences": [
                {"apiVersion": "v1", "kind": "Namespace", "name": "default", "uid": "0"}
            ]
        },
        {"finalizers": ["example.test/block"]},
        {"labels": {"app.kubernetes.io/managed-by": "other"}},
        {"annotations": {"example.test/note": "x"}},
    ],
    ids=lambda metadata: next(iter(metadata)),
)
def test_control_scope_refuses_metadata_changes_of_the_node_reader_binding(
    cluster: Cluster, metadata: dict[str, Any]
) -> None:
    binding = _node_reader(_OPENSHELL)
    binding["metadata"].update(metadata)

    _expect(cluster, CONTROL, "apply", binding, _CONTROL_SCOPE, _NODE_READER)


_SERVICES = "ValidatingAdmissionPolicy 'heartwood-session-services'"
_SERVICE = "has the type ClusterIP and no external address"


def _service(**spec: Any) -> dict[str, Any]:  # noqa: ANN401
    service = _named("Service", "case", _NAMESPACE)
    service["spec"] = {"selector": {"app": "case"}, "ports": [{"port": 8080}], **spec}
    return service


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param(_service(), None, id="cluster-ip"),
        pytest.param(_service(clusterIP="None"), None, id="headless"),
        pytest.param(_service(externalIPs=["203.0.113.7"]), _SERVICE, id="external-address"),
        pytest.param(_service(type="NodePort"), _SERVICE, id="node-port"),
        pytest.param(_service(type="LoadBalancer"), _SERVICE, id="load-balancer"),
        pytest.param(
            {**_service(), "spec": {"type": "ExternalName", "externalName": "example.test"}},
            _SERVICE,
            id="external-name",
        ),
    ],
)
def test_session_services_stay_inside_the_cluster_network(
    cluster: Cluster, document: dict[str, Any], message: str | None
) -> None:
    # The control service holds the right to create Services in a session namespace
    # once its role is bound there, as the fixture does for this test.
    _expect(cluster, CONTROL, "create", document, _SERVICES, message)


def test_control_service_may_review_tokens(cluster: Cluster) -> None:
    review = {
        "apiVersion": "authentication.k8s.io/v1",
        "kind": "TokenReview",
        "spec": {"token": "x"},
    }
    result = cluster.kubectl(
        "create",
        "--validate=false",
        "--filename",
        "-",
        "--output",
        "jsonpath={.status.error}",
        actor=CONTROL,
        document=review,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout


_SERVICE_ACCOUNT = "ServiceAccount hw-staging without a mounted token"
_IMAGE = "runs only the staging image"
_SECURITY = "read-only root file system and no capabilities"
_VOLUMES = "mounts only attempt volume claims and emptyDir volumes"
_CLAIM = "uses the StorageClass heartwood-attempt and names no data source or volume"
_ENVIRONMENT = "takes no environment value from another object or field"
_PLACEMENT = "names no node, priority class or runtime class"
_EXEC = "opens exec sessions only into pods named hw-staging-*"

_CONTAINER = "spec.containers.0"
_SIDECAR = {
    "name": "sidecar",
    "image": "docker.io/library/busybox:1.37",
    "securityContext": {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    },
}


_PLAIN = {"name": "KEY", "value": "value"}


def _pod_cases(image: str) -> list[tuple[str, dict[str, Any], str | None]]:
    pod = _pod(image)
    return [
        ("pod", pod, None),
        ("pod-restricted-namespace", _pod(image, namespace=_NAMESPACE), None),
        ("pod-plain-environment", _changed(pod, f"{_CONTAINER}.env", [_PLAIN]), None),
        (
            "pod-other-account",
            _changed(pod, "spec.serviceAccountName", "default"),
            _SERVICE_ACCOUNT,
        ),
        ("pod-default-account", _changed(pod, "spec.serviceAccountName"), _SERVICE_ACCOUNT),
        (
            "pod-mounted-token",
            _changed(pod, "spec.automountServiceAccountToken", True),
            _SERVICE_ACCOUNT,
        ),
        ("pod-default-token", _changed(pod, "spec.automountServiceAccountToken"), _SERVICE_ACCOUNT),
        (
            "pod-other-image",
            _changed(pod, f"{_CONTAINER}.image", "docker.io/library/busybox:1.37"),
            _IMAGE,
        ),
        ("pod-init-container", _changed(pod, "spec.initContainers", [_SIDECAR]), _IMAGE),
        (
            "pod-second-container",
            _changed(pod, "spec.containers", [*pod["spec"]["containers"], _SIDECAR]),
            _IMAGE,
        ),
        ("pod-root", _changed(pod, "spec.securityContext.runAsNonRoot"), _SECURITY),
        (
            "pod-container-root",
            _changed(pod, f"{_CONTAINER}.securityContext.runAsNonRoot", False),
            _SECURITY,
        ),
        (
            "pod-unconfined",
            _changed(pod, "spec.securityContext.seccompProfile", {"type": "Unconfined"}),
            _SECURITY,
        ),
        (
            "pod-container-unconfined",
            _changed(pod, f"{_CONTAINER}.securityContext.seccompProfile", {"type": "Unconfined"}),
            _SECURITY,
        ),
        ("pod-host-network", _changed(pod, "spec.hostNetwork", True), _SECURITY),
        ("pod-host-pid", _changed(pod, "spec.hostPID", True), _SECURITY),
        ("pod-host-ipc", _changed(pod, "spec.hostIPC", True), _SECURITY),
        (
            "pod-privilege-escalation",
            _changed(pod, f"{_CONTAINER}.securityContext.allowPrivilegeEscalation", True),
            _SECURITY,
        ),
        (
            "pod-writable-root",
            _changed(pod, f"{_CONTAINER}.securityContext.readOnlyRootFilesystem", False),
            _SECURITY,
        ),
        (
            "pod-capabilities-kept",
            _changed(pod, f"{_CONTAINER}.securityContext.capabilities"),
            _SECURITY,
        ),
        (
            "pod-capability-added",
            _changed(
                pod,
                f"{_CONTAINER}.securityContext.capabilities",
                {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]},
            ),
            _SECURITY,
        ),
        (
            "pod-secret-volume",
            _changed(
                pod,
                "spec.volumes.1",
                {"name": "v", "secret": {"secretName": "openshell-client-tls"}},
            ),
            _VOLUMES,
        ),
        (
            "pod-config-map-volume",
            _changed(
                pod, "spec.volumes.1", {"name": "v", "configMap": {"name": "openshell-config"}}
            ),
            _VOLUMES,
        ),
        (
            "pod-host-path-volume",
            _changed(pod, "spec.volumes.1", {"name": "v", "hostPath": {"path": "/"}}),
            _VOLUMES,
        ),
        (
            "pod-projected-volume",
            _changed(
                pod,
                "spec.volumes.1",
                {
                    "name": "v",
                    "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]},
                },
            ),
            _VOLUMES,
        ),
        (
            "pod-ephemeral-volume",
            _changed(
                pod,
                "spec.volumes.1",
                {"name": "v", "ephemeral": {"volumeClaimTemplate": {"spec": _claim()["spec"]}}},
            ),
            _VOLUMES,
        ),
        (
            "pod-secret-environment",
            _changed(
                pod,
                f"{_CONTAINER}.env",
                [
                    {
                        "name": "KEY",
                        "valueFrom": {"secretKeyRef": {"name": "openshell-kek", "key": "k"}},
                    }
                ],
            ),
            _ENVIRONMENT,
        ),
        (
            "pod-config-map-environment",
            _changed(
                pod,
                f"{_CONTAINER}.env",
                [
                    {
                        "name": "KEY",
                        "valueFrom": {"configMapKeyRef": {"name": "openshell-config", "key": "k"}},
                    }
                ],
            ),
            _ENVIRONMENT,
        ),
        (
            "pod-secret-environment-source",
            _changed(pod, f"{_CONTAINER}.envFrom", [{"secretRef": {"name": "openshell-kek"}}]),
            _ENVIRONMENT,
        ),
        ("pod-node-name", _changed(pod, "spec.nodeName", "node"), _PLACEMENT),
        (
            "pod-priority-class",
            _changed(pod, "spec.priorityClassName", "system-node-critical"),
            _PLACEMENT,
        ),
        ("pod-runtime-class", _changed(pod, "spec.runtimeClassName", "hw-policy-test"), _PLACEMENT),
        (
            "pod-other-claim",
            _changed(
                pod, "spec.volumes.0.persistentVolumeClaim.claimName", "openshell-data-openshell-0"
            ),
            _VOLUMES,
        ),
    ]


def test_staging_shape_pods(cluster: Cluster, staging_image: str) -> None:
    failures = []
    for name, document, message in _pod_cases(staging_image):
        try:
            _expect(cluster, _GATEWAY, "create", document, _STAGING_SHAPE, message)
        except AssertionError as error:
            failures.append(f"{name}: {error}")
    assert failures == []


_SOURCE = {"kind": "PersistentVolumeClaim", "name": "openshell-data-openshell-0"}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param(_claim(), None, id="claim"),
        pytest.param(_claim("inputs"), _CLAIM, id="claim-other-name"),
        pytest.param(
            _changed(_claim(), "spec.storageClassName", "standard"), _CLAIM, id="claim-other-class"
        ),
        pytest.param(_changed(_claim(), "spec.storageClassName"), _CLAIM, id="claim-default-class"),
        pytest.param(
            _changed(_claim(), "spec.dataSource", _SOURCE), _CLAIM, id="claim-data-source"
        ),
        pytest.param(
            _changed(_claim(), "spec.dataSourceRef", _SOURCE), _CLAIM, id="claim-data-source-ref"
        ),
        pytest.param(_changed(_claim(), "spec.volumeName", "pv"), _CLAIM, id="claim-volume-name"),
        pytest.param(
            _changed(_claim(), "spec.selector", {"matchLabels": {"a": "b"}}),
            _CLAIM,
            id="claim-selector",
        ),
    ],
)
def test_staging_shape_claims(
    cluster: Cluster, document: dict[str, Any], message: str | None
) -> None:
    _expect(cluster, _GATEWAY, "create", document, _STAGING_SHAPE, message)


def test_staging_shape_refuses_changing_a_pod_to_another_image(cluster: Cluster) -> None:
    result = cluster.kubectl(
        "--namespace",
        _NAMESPACE,
        "set",
        "image",
        "pod/hw-staging-probe",
        "staging=docker.io/library/busybox:1.37",
        "--dry-run=server",
        actor=_GATEWAY,
    )
    assert result.returncode != 0
    assert _STAGING_SHAPE in result.stderr, result.stderr
    assert _IMAGE in result.stderr, result.stderr


def test_staging_shape_admits_exec_into_staging_pods_only(cluster: Cluster) -> None:
    def connect(*arguments: str) -> tuple[int, str]:
        result = cluster.kubectl("--namespace", _NAMESPACE, *arguments, actor=_GATEWAY)
        return result.returncode, result.stderr

    code, error = connect("exec", "hw-staging-probe", "--", "/agnhost", "help")
    assert code == 0, error
    for arguments in (
        ("exec", "other-probe", "--", "/agnhost", "help"),
        ("attach", "hw-staging-probe"),
        ("port-forward", "pod/hw-staging-probe", ":8080"),
    ):
        code, error = connect(*arguments)
        assert code != 0, arguments
        assert _STAGING_SHAPE in error, error
        assert _EXEC in error, error
