# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import FakeKubernetes
from heartwood_platform.common.kubernetes import Kubernetes
from heartwood_platform.control.sessions import (
    ApiServerAddress,
    SessionError,
    Sessions,
    SessionSettings,
)

_OBJECTS = (
    Path(__file__).resolve().parents[2]
    / "chart"
    / "heartwood-platform"
    / "files"
    / "openshell-session.json"
)
_IMAGE = "registry.example.test/openshell-gateway@sha256:" + "1" * 64
_RBAC = "/apis/rbac.authorization.k8s.io/v1"


class Platform:
    def __init__(self) -> None:
        self.api = FakeKubernetes()
        self.active: list[str] = []
        self.now = 0.0
        self.settings = SessionSettings(
            control_namespace="heartwood-platform",
            control_account="heartwood-control",
            gateway_namespace="heartwood-sessions",
            openshell_objects=_OBJECTS.read_text(encoding="utf-8"),
            openshell_gateway_image=_IMAGE,
            api_server=(
                ApiServerAddress("10.96.0.1/32", 443),
                ApiServerAddress("172.18.0.2/32", 6443),
            ),
            quota={"pods": "20"},
            container_defaults={"cpu": "500m", "memory": "512Mi"},
            container_default_requests={"cpu": "100m", "memory": "128Mi"},
            ready_timeout_seconds=10,
        )
        self.sessions = Sessions(
            Kubernetes(self.api.client()),
            self.settings,
            lambda: self.active,
            sleep=self._sleep,
            clock=lambda: self.now,
        )

    def _sleep(self, seconds: float) -> None:
        self.now += seconds

    def provision(self, session: str) -> Any:  # noqa: ANN401
        if session not in self.active:
            self.active.append(session)
        return self.sessions.provision(session)

    def node_reader_subjects(self) -> list[dict[str, str]]:
        binding = self.api.objects[f"{_RBAC}/clusterrolebindings/heartwood-openshell-node-reader"]
        subjects = binding["subjects"]
        assert isinstance(subjects, list)
        return subjects


@pytest.fixture
def platform() -> Platform:
    return Platform()


def _account(name: str, namespace: str) -> dict[str, str]:
    return {"kind": "ServiceAccount", "name": name, "namespace": namespace}


def test_namespace_is_created_restricted_with_quota_limits_and_default_deny(
    platform: Platform,
) -> None:
    provisioned = platform.provision("abc123")

    namespace = platform.api.objects["/api/v1/namespaces/hw-s-abc123"]
    assert namespace["metadata"]["labels"] == {
        "heartwood.platform/session": "abc123",
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
    }
    assert provisioned.namespace == "hw-s-abc123"
    assert provisioned.namespace_uid == namespace["metadata"]["uid"]
    assert provisioned.kube_system_uid == "uid-0"
    assert platform.api.kind("ResourceQuota", "hw-s-abc123")["session"]["spec"] == {
        "hard": {"pods": "20"}
    }
    assert set(platform.api.kind("LimitRange", "hw-s-abc123")) == {"session"}
    policies = platform.api.kind("NetworkPolicy", "hw-s-abc123")
    assert policies["hw-default-deny"]["spec"] == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
    }
    (rule,) = policies["hw-openshell-gateway-ingress"]["spec"]["ingress"]
    assert rule["from"][1] == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "heartwood-sessions"}},
        "podSelector": {"matchLabels": {"heartwood.platform/session": "abc123"}},
    }
    egress = policies["hw-openshell-api-egress"]["spec"]["egress"]
    assert [rule["to"][0].get("ipBlock") for rule in egress[1:]] == [
        {"cidr": "10.96.0.1/32"},
        {"cidr": "172.18.0.2/32"},
    ]
    assert [rule["ports"][0]["port"] for rule in egress[1:]] == [443, 6443]


def test_each_fixed_role_is_bound_to_its_only_subject_with_role_bindings(
    platform: Platform,
) -> None:
    platform.provision("abc123")

    bindings = platform.api.kind("RoleBinding", "hw-s-abc123")
    assert {
        name: (binding["roleRef"], binding["subjects"]) for name, binding in bindings.items()
    } == {
        role: (
            {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
            [subject],
        )
        for role, subject in {
            "heartwood-session-control": _account("heartwood-control", "heartwood-platform"),
            "heartwood-openshell-sandbox": _account("openshell", "hw-s-abc123"),
            "heartwood-openshell-peer": _account("openshell", "hw-s-abc123"),
            "heartwood-openshell-certgen": _account("openshell-certgen", "hw-s-abc123"),
            "heartwood-session-gateway": _account("hw-gw-abc123", "heartwood-sessions"),
        }.items()
    }
    # The only cluster-scoped writes are the namespace and the node reader subjects.
    cluster_scoped = {path for method, path in platform.api.calls if method != "GET"}
    assert {path for path in cluster_scoped if "/namespaces/" not in path} == {
        "/api/v1/namespaces",
        f"{_RBAC}/clusterrolebindings/heartwood-openshell-node-reader",
    }
    assert platform.node_reader_subjects() == [_account("openshell", "hw-s-abc123")]
    assert set(platform.api.kind("ServiceAccount", "heartwood-sessions")) == {"hw-gw-abc123"}


def test_openshell_objects_are_applied_in_the_namespace_with_the_configured_image(
    platform: Platform,
) -> None:
    provisioned = platform.provision("abc123")

    rendered = json.loads(_OBJECTS.read_text(encoding="utf-8"))["items"]
    applied = [
        item
        for item in platform.api.objects.values()
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/instance")
        == "openshell"
    ]
    assert len(applied) == len(rendered)
    text = json.dumps(applied)
    assert "placeholder" not in text
    assert {item["metadata"]["namespace"] for item in applied} == {"hw-s-abc123"}
    assert text.count(_IMAGE) == 2
    assert "openshell.hw-s-abc123.svc.cluster.local" in text
    assert provisioned.openshell_endpoint == "https://openshell.hw-s-abc123.svc.cluster.local:8080"
    assert provisioned.openshell_client == {
        "ca.crt": "ca.crt of hw-s-abc123",
        "tls.crt": "tls.crt of hw-s-abc123",
        "tls.key": "tls.key of hw-s-abc123",
    }


def test_each_session_gets_its_own_credential_storage_key_and_keeps_it(
    platform: Platform,
) -> None:
    def key(session: str) -> bytes:
        secret = platform.api.kind("Secret", f"hw-s-{session}")["openshell-kek"]
        assert secret["type"] == "Opaque"
        return base64.b64decode(base64.b64decode(secret["data"]["key-encryption-key"]))

    platform.provision("abc123")
    platform.provision("def456")
    first = key("abc123")

    assert len(first) == 32
    assert key("def456") != first

    assert platform.provision("abc123") == platform.provision("abc123")
    assert key("abc123") == first


def test_node_reader_subjects_follow_the_sessions_and_removal_deletes_everything(
    platform: Platform,
) -> None:
    platform.provision("abc123")
    platform.provision("def456")
    platform.sessions.write_gateway_secret("abc123", {"launch.json": "{}"})
    assert platform.node_reader_subjects() == [
        _account("openshell", "hw-s-abc123"),
        _account("openshell", "hw-s-def456"),
    ]

    platform.sessions.remove("abc123")
    platform.sessions.remove("abc123")

    assert platform.node_reader_subjects() == [_account("openshell", "hw-s-def456")]
    assert [path for path in platform.api.objects if "abc123" in path] == []
    # The registry lists a session until its removal is complete. A launch of another
    # session in that time does not add the removed subject again.
    assert "abc123" in platform.active
    platform.provision("def456")
    assert platform.node_reader_subjects() == [_account("openshell", "hw-s-def456")]
    assert set(platform.api.kind("ServiceAccount", "heartwood-sessions")) == {"hw-gw-def456"}
    assert "/api/v1/namespaces/hw-s-def456" in platform.api.objects


def test_gateway_secret_is_replaced(platform: Platform) -> None:
    platform.sessions.write_gateway_secret("abc123", {"launch.json": "first"})
    platform.sessions.write_gateway_secret("abc123", {"launch.json": "second"})

    secret = platform.api.kind("Secret", "heartwood-sessions")["hw-gw-abc123"]
    assert secret["type"] == "Opaque"
    assert base64.b64decode(secret["data"]["launch.json"]) == b"second"


def test_foreign_namespace_of_the_same_name_is_not_used(platform: Platform) -> None:
    platform.api.objects["/api/v1/namespaces/hw-s-abc123"] = {
        "kind": "Namespace",
        "metadata": {"name": "hw-s-abc123", "uid": "foreign", "labels": {}},
    }

    with pytest.raises(SessionError, match="does not carry the session labels"):
        platform.provision("abc123")

    assert platform.api.kind("Secret", "hw-s-abc123") == {}
    assert platform.api.kind("StatefulSet", "hw-s-abc123") == {}


def test_launch_fails_when_the_gateway_does_not_become_ready(platform: Platform) -> None:
    platform.api.reads_until_ready = 1000

    with pytest.raises(SessionError, match="the OpenShell gateway is not ready"):
        platform.provision("abc123")

    assert platform.now >= 10
    platform.api.reads_until_ready = 0
    assert platform.provision("abc123").namespace == "hw-s-abc123"


@pytest.mark.parametrize("status", [404, 409])
def test_deleting_an_absent_or_terminating_object_succeeds(status: int) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status))
    kubernetes = Kubernetes(httpx.Client(transport=transport, base_url="https://k.example.test"))

    kubernetes.delete("Namespace", "hw-s-abc123")


def test_gateway_secret_that_another_party_created_fails_the_launch(platform: Platform) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(409 if request.method == "POST" else 404)
    )
    sessions = Sessions(
        Kubernetes(httpx.Client(transport=transport, base_url="https://k.example.test")),
        platform.settings,
        list,
    )

    with pytest.raises(SessionError, match="created by another party"):
        sessions.write_gateway_secret("abc123", {"launch.json": "{}"})
