# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reachability of the platform components under the chart's network policies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from kube import Cluster

_PLATFORM = "heartwood-platform.svc.cluster.local"
_KEYCLOAK = f"keycloak.{_PLATFORM}:8443"
_POSTGRESQL = f"postgresql.{_PLATFORM}:5432"
_VERSITYGW = f"versitygw.{_PLATFORM}:7070"
_CONTROL = f"heartwood-control.{_PLATFORM}:8080"
# Consecutive timeouts that count as blocked, as in network-policy-controls.sh.
_BLOCKED_AFTER = 3


def _probe(namespace: str, image: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "hw-network-probe", "namespace": namespace},
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
                }
            ],
        },
    }


@pytest.fixture(scope="module", autouse=True)
def probes(cluster: Cluster, probe_image: str) -> Iterator[None]:
    namespaces = ("default", "heartwood-sessions")
    cluster.apply(*(_probe(namespace, probe_image) for namespace in namespaces))
    for namespace in namespaces:
        ready = cluster.kubectl(
            "wait", f"--namespace={namespace}", "--for=condition=Ready", "pod/hw-network-probe"
        )
        assert ready.returncode == 0, ready.stderr
    yield
    for namespace in namespaces:
        cluster.kubectl("delete", f"--namespace={namespace}", "pod/hw-network-probe", "--wait")


def _connect(cluster: Cluster, namespace: str, target: str) -> str:
    result = cluster.kubectl(
        "exec",
        f"--namespace={namespace}",
        "hw-network-probe",
        "--",
        "/agnhost",
        "connect",
        "--timeout=3s",
        target,
    )
    if result.returncode == 0:
        return "connected"
    return "timeout" if "TIMEOUT" in result.stdout + result.stderr else "error"


def _reachable(cluster: Cluster, namespace: str, target: str) -> bool:
    """Report a connection, or a block after consecutive timeouts."""
    timeouts = 0
    for _ in range(10):
        outcome = _connect(cluster, namespace, target)
        if outcome == "connected":
            return True
        timeouts = timeouts + 1 if outcome == "timeout" else 0
        if timeouts == _BLOCKED_AFTER:
            return False
    pytest.fail(f"no conclusive result for {target} from {namespace}")


@pytest.mark.parametrize("target", [_KEYCLOAK, _CONTROL])
def test_sessions_namespace_reaches_keycloak_and_the_control_service(
    cluster: Cluster, target: str
) -> None:
    assert _reachable(cluster, "heartwood-sessions", target)


@pytest.mark.parametrize(
    ("namespace", "target"),
    [
        ("heartwood-sessions", _POSTGRESQL),
        ("heartwood-sessions", _VERSITYGW),
        ("default", _KEYCLOAK),
        ("default", _POSTGRESQL),
        ("default", _VERSITYGW),
        ("default", _CONTROL),
    ],
)
def test_other_connections_to_platform_components_are_blocked(
    cluster: Cluster, namespace: str, target: str
) -> None:
    # Allowed connections show that both probes and the name resolution work.
    assert _reachable(cluster, "heartwood-sessions", _KEYCLOAK)
    assert _reachable(cluster, namespace, "kubernetes.default.svc.cluster.local:443")
    assert not _reachable(cluster, namespace, target)
