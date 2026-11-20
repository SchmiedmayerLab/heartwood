# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session namespaces that the control service provisions with its own credentials.

A ServiceAccount hub in heartwood-sessions stands in for the spawner. The negative
cases send requests from inside the control service's pod, with its token.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from kube import PROBE_CA, Cluster

_SESSIONS = "heartwood-sessions"
_PLATFORM = "heartwood-platform"
_CONTROL = "https://heartwood-control.heartwood-platform.svc.cluster.local:8443"
_FIRST = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001"
_SECOND = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0002"
_POLICY = "ValidatingAdmissionPolicy 'heartwood-control-scope'"
_RBAC = "rbac.authorization.k8s.io"


def _pod(name: str, image: str, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": _SESSIONS, "labels": labels},
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "agnhost",
                    "image": image,
                    "args": ["pause"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "platform-ca", "mountPath": "/etc/platform-ca"}],
                }
            ],
            "volumes": [{"name": "platform-ca", "configMap": {"name": PROBE_CA}}],
        },
    }


def _json(cluster: Cluster, *arguments: str) -> Any:  # noqa: ANN401
    result = cluster.kubectl("get", *arguments, "--output=json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _names(cluster: Cluster, kind: str, namespace: str) -> set[str]:
    items = _json(cluster, kind, f"--namespace={namespace}")["items"]
    return {item["metadata"]["name"] for item in items}


def _call(cluster: Cluster, method: str, subject: str, project: str) -> tuple[int, Any]:
    token = cluster.kubectl(
        "create", "token", "hub", f"--namespace={_PLATFORM}", "--audience=heartwood-control"
    )
    assert token.returncode == 0, token.stderr
    result = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={_SESSIONS}",
        "hw-session-probe",
        "--",
        "curl",
        "--silent",
        "--max-time",
        "240",
        "--cacert",
        "/etc/platform-ca/ca.crt",
        "--request",
        method,
        "--header",
        "@-",
        "--write-out",
        "\n%{http_code}",
        f"{_CONTROL}/v1/researchers/{subject}/projects/{project}/launch",
        text_input=f"Authorization: Bearer {token.stdout.strip()}",
    )
    assert result.returncode == 0, result.stderr
    body, _, status = result.stdout.rpartition("\n")
    return int(status), json.loads(body) if body else None


@pytest.fixture(scope="module")
def launch(cluster: Cluster, probe_image: str) -> Iterator[dict[str, Any]]:
    """Launch one session and delete every session of the module afterwards."""
    cluster.apply(_pod("hw-session-probe", probe_image, {}))
    ready = cluster.kubectl(
        "wait", f"--namespace={_SESSIONS}", "--for=condition=Ready", "pod/hw-session-probe"
    )
    assert ready.returncode == 0, ready.stderr
    status, body = _call(cluster, "PUT", _FIRST, "demo")
    assert status == 200, body
    yield body
    for subject in (_FIRST, _SECOND):
        _call(cluster, "DELETE", subject, "demo")
    cluster.kubectl(
        "delete",
        f"--namespace={_SESSIONS}",
        "pod",
        "--wait",
        "--ignore-not-found",
        "hw-session-probe",
        "hw-session-gateway",
        "hw-session-other",
    )


def test_namespace_is_restricted_with_quota_limits_and_default_deny(
    cluster: Cluster, launch: dict[str, Any]
) -> None:
    namespace = launch["namespace"]
    found = _json(cluster, "namespace", namespace)

    assert namespace == f"hw-s-{launch['session']}"
    assert found["metadata"]["uid"] == launch["namespace_uid"]
    assert found["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    assert found["metadata"]["labels"]["heartwood.platform/session"] == launch["session"]
    assert (
        _json(cluster, "namespace", "kube-system")["metadata"]["uid"] == launch["kube_system_uid"]
    )
    assert _names(cluster, "resourcequota", namespace) == {"session"}
    assert _names(cluster, "limitrange", namespace) == {"session"}
    assert {
        "hw-default-deny",
        "hw-openshell-gateway-ingress",
        "hw-openshell-api-egress",
        "openshell-sandbox-ssh",
    } <= _names(cluster, "networkpolicy", namespace)
    assert _names(cluster, "role", namespace) == set()


def test_namespace_holds_the_staging_account_and_the_platform_ca(
    cluster: Cluster, launch: dict[str, Any]
) -> None:
    namespace = launch["namespace"]

    staging = _json(cluster, "serviceaccount", "hw-staging", f"--namespace={namespace}")
    assert staging["automountServiceAccountToken"] is False
    copied = _json(cluster, "configmap", "platform-ca", f"--namespace={namespace}")
    platform = _json(cluster, "configmap", "platform-ca", f"--namespace={_PLATFORM}")
    assert copied["data"]["ca.crt"] == platform["data"]["ca.crt"]


def test_fixed_roles_are_bound_to_the_session_accounts_only(
    cluster: Cluster, launch: dict[str, Any]
) -> None:
    namespace, session = launch["namespace"], launch["session"]

    def account(name: str, where: str) -> list[dict[str, str]]:
        return [{"kind": "ServiceAccount", "name": name, "namespace": where}]

    bindings = _json(cluster, "rolebinding", f"--namespace={namespace}")["items"]
    assert {
        binding["metadata"]["name"]: (binding["roleRef"]["kind"], binding["subjects"])
        for binding in bindings
    } == {
        "heartwood-session-control": ("ClusterRole", account("heartwood-control", _PLATFORM)),
        "heartwood-openshell-sandbox": ("ClusterRole", account("openshell", namespace)),
        "heartwood-openshell-peer": ("ClusterRole", account("openshell", namespace)),
        "heartwood-openshell-certgen": ("ClusterRole", account("openshell-certgen", namespace)),
        "heartwood-session-gateway": ("ClusterRole", account(f"hw-gw-{session}", _SESSIONS)),
    }
    node_reader = _json(cluster, "clusterrolebinding", "heartwood-openshell-node-reader")
    assert account("openshell", namespace)[0] in node_reader["subjects"]
    assert launch["gateway_service_account"] == f"hw-gw-{session}"
    assert f"hw-gw-{session}" in _names(cluster, "serviceaccount", _SESSIONS)

    gateway = f"system:serviceaccount:{_SESSIONS}:hw-gw-{session}"
    for where, expected in ((namespace, "yes"), ("default", "no"), (_PLATFORM, "no")):
        allowed = cluster.kubectl(
            "auth", "can-i", "create", "pods", f"--namespace={where}", actor=gateway
        )
        assert allowed.stdout.strip() == expected, where


def test_openshell_gateway_is_ready_with_its_own_key_and_client_bundle(
    cluster: Cluster, launch: dict[str, Any]
) -> None:
    namespace = launch["namespace"]

    gateway = _json(cluster, "statefulset", "openshell", f"--namespace={namespace}")
    assert gateway["status"]["readyReplicas"] == 1
    key = _json(cluster, "secret", "openshell-kek", f"--namespace={namespace}")
    assert len(base64.b64decode(base64.b64decode(key["data"]["key-encryption-key"]))) == 32

    secret = _json(cluster, "secret", launch["gateway_service_account"], f"--namespace={_SESSIONS}")
    files = {name: base64.b64decode(value).decode() for name, value in secret["data"].items()}
    assert set(files) == {
        "launch.json",
        "keycloak-client-secret",
        "openshell-ca.crt",
        "openshell-tls.crt",
        "openshell-tls.key",
        "platform-ca.crt",
        "deployment.toml",
        "kubeconfig",
        "worker-policy.json",
        "launch-context.json",
    }
    platform_ca = _json(cluster, "configmap", "platform-ca", f"--namespace={_PLATFORM}")
    assert files["platform-ca.crt"] == platform_ca["data"]["ca.crt"]
    assert json.loads(files["launch.json"]) == launch
    bundle = _json(cluster, "secret", "openshell-client-tls", f"--namespace={namespace}")
    assert files["openshell-tls.crt"] == base64.b64decode(bundle["data"]["tls.crt"]).decode()
    assert launch["openshell_endpoint"] == f"https://openshell.{namespace}.svc.cluster.local:8080"
    for credential in (files["keycloak-client-secret"], files["openshell-tls.key"]):
        assert credential not in json.dumps(launch)


def test_relaunch_keeps_the_namespace_the_key_and_the_running_gateway(
    cluster: Cluster, launch: dict[str, Any]
) -> None:
    namespace = launch["namespace"]

    def state() -> tuple[str, str, str]:
        key = _json(cluster, "secret", "openshell-kek", f"--namespace={namespace}")
        pod = _json(cluster, "pod", "openshell-0", f"--namespace={namespace}")
        gateway = _json(cluster, "statefulset", "openshell", f"--namespace={namespace}")
        return (
            key["metadata"]["resourceVersion"],
            pod["metadata"]["uid"],
            str(gateway["metadata"]["generation"]),
        )

    before = state()
    # The certificate Job is removed some time after it finishes. A relaunch after
    # that applies it again.
    cluster.kubectl("delete", f"--namespace={namespace}", "job/openshell-certgen", "--wait")
    status, again = _call(cluster, "PUT", _FIRST, "demo")

    assert status == 200, again
    assert {**again, "launch_context": None} == {**launch, "launch_context": None}
    assert state() == before


def test_sessions_are_separate_and_the_node_reader_follows_them(
    cluster: Cluster, launch: dict[str, Any]
) -> None:
    status, second = _call(cluster, "PUT", _SECOND, "demo")
    assert status == 200, second
    assert second["namespace"] != launch["namespace"]

    def subjects() -> set[str]:
        binding = _json(cluster, "clusterrolebinding", "heartwood-openshell-node-reader")
        return {subject["namespace"] for subject in binding.get("subjects", [])}

    assert {launch["namespace"], second["namespace"]} <= subjects()
    gateway = f"system:serviceaccount:{_SESSIONS}:{second['gateway_service_account']}"
    allowed = cluster.kubectl(
        "auth", "can-i", "get", "pods", f"--namespace={launch['namespace']}", actor=gateway
    )
    assert allowed.stdout.strip() == "no"

    assert _call(cluster, "DELETE", _SECOND, "demo")[0] == 204
    gone = cluster.kubectl(
        "wait", "--for=delete", f"namespace/{second['namespace']}", "--timeout=180s"
    )
    assert gone.returncode == 0, gone.stderr
    assert subjects() & {launch["namespace"], second["namespace"]} == {launch["namespace"]}
    assert second["gateway_service_account"] not in _names(cluster, "serviceaccount", _SESSIONS)
    assert second["gateway_service_account"] not in _names(cluster, "secret", _SESSIONS)
    assert _json(cluster, "statefulset", "openshell", f"--namespace={launch['namespace']}")


def test_only_the_gateway_pod_of_the_session_reaches_the_openshell_gateway(
    cluster: Cluster, launch: dict[str, Any], probe_image: str
) -> None:
    label = "heartwood.platform/session"
    cluster.apply(
        _pod("hw-session-gateway", probe_image, {label: launch["session"]}),
        _pod("hw-session-other", probe_image, {label: "0000000000000000"}),
    )
    ready = cluster.kubectl(
        "wait",
        f"--namespace={_SESSIONS}",
        "--for=condition=Ready",
        "pod",
        "hw-session-gateway",
        "hw-session-other",
    )
    assert ready.returncode == 0, ready.stderr
    target = f"openshell.{launch['namespace']}.svc.cluster.local:8080"

    def outcomes(pod: str) -> list[str]:
        found = []
        for _ in range(3):
            result = cluster.kubectl(
                "exec",
                f"--namespace={_SESSIONS}",
                pod,
                "--",
                "/agnhost",
                "connect",
                "--timeout=3s",
                target,
            )
            found.append(
                "connected"
                if result.returncode == 0
                else "timeout"
                if "TIMEOUT" in result.stdout + result.stderr
                else "error"
            )
        return found

    assert "connected" in outcomes("hw-session-gateway")
    assert outcomes("hw-session-other") == ["timeout"] * 3
    assert outcomes("hw-session-probe") == ["timeout"] * 3


_ATTEMPT = """
import json, sys
from heartwood_platform.common.kubernetes import in_cluster_client
path, document = json.load(sys.stdin)
response = in_cluster_client().post(path, json=document)
print(json.dumps([response.status_code, response.json().get("message", "")]))
"""


def _as_control_service(cluster: Cluster, path: str, document: dict[str, Any]) -> tuple[int, str]:
    """Send a create request from the control service's pod with its own token."""
    result = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={_PLATFORM}",
        "heartwood-control-0",
        "--",
        "/opt/heartwood-platform/.venv/bin/python",
        "-I",
        "-c",
        _ATTEMPT,
        text_input=json.dumps([path, document]),
    )
    assert result.returncode == 0, result.stderr
    status, message = json.loads(result.stdout)
    return status, message


def _binding(namespace: str, role: str, subject: dict[str, str]) -> tuple[str, dict[str, Any]]:
    return (
        f"/apis/{_RBAC}/v1/namespaces/{namespace}/rolebindings",
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "hw-negative-case", "namespace": namespace},
            "roleRef": {"apiGroup": _RBAC, "kind": "ClusterRole", "name": role},
            "subjects": [subject],
        },
    )


def test_control_service_credentials_are_confined_by_the_admission_policy(
    cluster: Cluster, launch: dict[str, Any]
) -> None:
    namespace = launch["namespace"]
    control = {"kind": "ServiceAccount", "name": "heartwood-control", "namespace": _PLATFORM}
    gateway = {"kind": "ServiceAccount", "name": "hw-gw-0000000000000000", "namespace": _SESSIONS}
    unlabelled = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "hw-s-negativecase",
            "labels": {"heartwood.platform/session": "negativecase"},
        },
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "hw-negative-case", "namespace": _SESSIONS},
    }

    other_name = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "hw-negative-case",
            "labels": {
                "heartwood.platform/session": "negative-case",
                "pod-security.kubernetes.io/enforce": "restricted",
            },
        },
    }

    cases = {
        "a namespace without the Pod Security label": ("/api/v1/namespaces", unlabelled),
        "a namespace that is not named like a session": ("/api/v1/namespaces", other_name),
        "a binding outside session namespaces": _binding(
            "default", "heartwood-session-control", control
        ),
        "its own role in the platform namespace": _binding(
            _PLATFORM, "heartwood-session-control", control
        ),
        "the gateway role for another session's gateway": _binding(
            namespace, "heartwood-session-gateway", gateway
        ),
        "the sandbox role for itself": _binding(namespace, "heartwood-openshell-sandbox", control),
        "another Secret in the sessions namespace": (
            f"/api/v1/namespaces/{_SESSIONS}/secrets",
            secret,
        ),
    }
    for case, (path, document) in cases.items():
        status, message = _as_control_service(cluster, path, document)
        assert status in {403, 422}, case
        assert _POLICY in message, (case, message)

    # Outside its bindings the service has no rights at all.
    config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "hw-negative-case"}}
    status, message = _as_control_service(
        cluster, "/api/v1/namespaces/default/configmaps", config_map
    )
    assert status == 403, message
    for name in ("hw-s-negativecase", "hw-negative-case"):
        assert cluster.kubectl("get", "namespace", name).returncode != 0
