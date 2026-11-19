# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Checks of the rendered chart. They need the helm and openssl commands."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[2] / "chart" / "heartwood-platform"
_DIGEST = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_TEMPLATE = ["helm", "template", "heartwood-platform", str(_CHART)]
_TEMPLATE += ["--namespace", "heartwood-platform"]
# The platform services image has no default.
_SERVICES = "images.platformServices=registry.example.test/services@sha256:" + "0" * 64
_GATEWAY = "images.openshellGateway=registry.example.test/gateway@sha256:" + "1" * 64
_IMAGES = ["--set", _SERVICES, "--set", _GATEWAY]
# Without a cluster, Helm cannot read the API server addresses.
_API_SERVER = ["--set-json", 'sessions.apiServer=[{"cidr":"10.96.0.1/32","port":443}]']
_IMAGES += _API_SERVER
_BASE = list(_TEMPLATE)
_TEMPLATE += _IMAGES
_FIXED_ROLES = {
    "heartwood-openshell-sandbox",
    "heartwood-openshell-peer",
    "heartwood-openshell-certgen",
    "heartwood-openshell-node-reader",
    "heartwood-session-gateway",
    "heartwood-session-control",
}


def _render(values: str) -> list[dict[str, Any]]:
    output = subprocess.run(
        [*_TEMPLATE, "--values", str(_CHART / values)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(output) if item is not None]


@pytest.fixture(scope="module", params=["values-kind.yaml", "values-gke.yaml"])
def objects(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    return _render(request.param)


def _named(objects: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    (found,) = [
        item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name
    ]
    return found


def _pod_specs(objects: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    for item in objects:
        if item["kind"] in {"Deployment", "StatefulSet", "Job"}:
            yield item["metadata"]["name"], item["spec"]["template"]["spec"]


@pytest.mark.parametrize("values", ["values-kind.yaml", "values-gke.yaml"])
def test_chart_passes_lint(values: str) -> None:
    subprocess.run(
        ["helm", "lint", str(_CHART), "--strict", *_IMAGES, "--values", str(_CHART / values)],
        check=True,
        capture_output=True,
    )


def test_every_image_is_pinned_by_digest(objects: list[dict[str, Any]]) -> None:
    images = [
        container["image"]
        for _, spec in _pod_specs(objects)
        for container in spec["containers"] + spec.get("initContainers", [])
    ]
    assert images
    assert [image for image in images if not _DIGEST.fullmatch(image)] == []


def test_schema_rejects_an_image_without_a_digest() -> None:
    result = subprocess.run(
        [*_TEMPLATE, "--set", "images.keycloak=keycloak:26.8.2"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "/images/keycloak" in result.stderr


def test_pods_meet_the_restricted_pod_security_standard(objects: list[dict[str, Any]]) -> None:
    for name, spec in _pod_specs(objects):
        assert spec["automountServiceAccountToken"] is (name == "heartwood-control"), name
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}, name
        for container in spec["containers"] + spec.get("initContainers", []):
            context = container["securityContext"]
            assert context["allowPrivilegeEscalation"] is False, name
            assert context["capabilities"] == {"drop": ["ALL"]}, name


def test_every_platform_namespace_denies_ingress_by_default(
    objects: list[dict[str, Any]],
) -> None:
    denied = {
        item["metadata"]["namespace"]
        for item in objects
        if item["kind"] == "NetworkPolicy"
        and item["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    }
    assert denied == {"heartwood-platform", "heartwood-sessions"}


def test_fixed_cluster_roles_grant_no_role_management(objects: list[dict[str, Any]]) -> None:
    roles = [item for item in objects if item["kind"] in {"ClusterRole", "Role"}]
    assert {role["metadata"]["name"] for role in roles} == _FIXED_ROLES | {"heartwood-control"}
    for role in roles:
        for rule in role["rules"]:
            assert "*" not in rule["verbs"] + rule["resources"] + rule["apiGroups"]
            assert "escalate" not in rule["verbs"]
            assert not {"roles", "clusterroles"} & set(rule["resources"]) or rule["verbs"] == [
                "bind"
            ]


def test_control_service_binds_only_the_fixed_cluster_roles(
    objects: list[dict[str, Any]],
) -> None:
    rules = _named(objects, "ClusterRole", "heartwood-control")["rules"]
    (bind,) = [rule for rule in rules if "bind" in rule["verbs"]]
    assert set(bind["resourceNames"]) == _FIXED_ROLES
    for rule in rules:
        if rule["resources"] == ["clusterrolebindings"]:
            assert rule["resourceNames"] == ["heartwood-openshell-node-reader"]
    binding = _named(objects, "ClusterRoleBinding", "heartwood-openshell-node-reader")
    assert "subjects" not in binding


@pytest.mark.parametrize(
    "name", ["heartwood-control-scope", "heartwood-staging-shape", "heartwood-session-services"]
)
def test_admission_policies_fail_closed_and_deny(objects: list[dict[str, Any]], name: str) -> None:
    assert _named(objects, "ValidatingAdmissionPolicy", name)["spec"]["failurePolicy"] == "Fail"
    binding = _named(objects, "ValidatingAdmissionPolicyBinding", name)
    assert binding["spec"] == {"policyName": name, "validationActions": ["Deny"]}


def test_credentials_are_generated_for_each_installation() -> None:
    def secrets(objects: list[dict[str, Any]]) -> dict[str, str]:
        return {
            f"{item['metadata']['name']}/{key}": value
            for item in objects
            if item["kind"] == "Secret"
            for key, value in item["data"].items()
            if key not in {"username", "access-key"}
        }

    first, second = secrets(_render("values-kind.yaml")), secrets(_render("values-kind.yaml"))
    assert len(first) >= 6
    configuration = _named(_render("values-kind.yaml"), "ConfigMap", "heartwood-control")
    assert not set(first.values()) & set(configuration["data"]["configuration.json"].split('"'))
    assert [key for key in first if first[key] == second[key]] == []


def test_realm_import_holds_scopes_and_no_credential(objects: list[dict[str, Any]]) -> None:
    config = _named(objects, "ConfigMap", "keycloak-realm")
    realm = json.loads(config["data"]["heartwood-realm.json"])
    scopes = {scope["name"] for scope in realm["clientScopes"]}
    assert {"basic", "kc-admin-api", "svc-control", "svc-data", "svc-jobs"} <= scopes
    clients = {client["clientId"]: client for client in realm["clients"]}
    assert clients["heartwood-control"]["secret"] == "${HEARTWOOD_CONTROL_CLIENT_SECRET}"
    assert clients["heartwood-control"]["optionalClientScopes"] == ["kc-admin-api"]
    (account,) = realm["users"]
    assert account["clientRoles"] == {"realm-management": ["manage-clients"]}
    projects = {scope for scope in scopes if scope.startswith("project-")}
    assert set(clients["heartwood-cli"]["optionalClientScopes"]) == projects


def test_platform_ca_certificate_is_a_certificate_authority(
    objects: list[dict[str, Any]],
) -> None:
    secret = _named(objects, "Secret", "platform-ca")
    certificate = base64.b64decode(secret["data"]["tls.crt"])
    text = subprocess.run(
        ["openssl", "x509", "-noout", "-text"], input=certificate, check=True, capture_output=True
    ).stdout.decode()
    assert "CA:TRUE" in text
    assert "Certificate Sign" in text
    assert "X509v3 Subject Key Identifier" in text
    published = _named(objects, "ConfigMap", "platform-ca")["data"]["ca.crt"]
    assert published.strip() == certificate.decode().strip()
    assert "PRIVATE KEY" not in published


def test_gke_values_set_only_cluster_specific_keys() -> None:
    values = yaml.safe_load((_CHART / "values-gke.yaml").read_text(encoding="utf-8"))
    assert set(values) <= {"storageClassName", "gpu", "sessions"}
    assert set(values.get("sessions", {})) <= {"attemptStorageClass"}


def test_release_must_use_the_platform_namespace() -> None:
    result = subprocess.run(
        ["helm", "template", "heartwood-platform", str(_CHART), "--namespace", "default"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "heartwood-platform" in result.stderr


@pytest.mark.parametrize("namespace", ["heartwood-platform", "hw-s-hub"])
def test_sessions_namespace_is_neither_the_platform_nor_a_session_namespace(namespace: str) -> None:
    result = subprocess.run(
        [*_TEMPLATE, "--set", f"sessions.namespace={namespace}"], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "/sessions/namespace" in result.stderr


@pytest.mark.parametrize(
    ("given", "missing"),
    [(_GATEWAY, "/images/platformServices"), (_SERVICES, "/images/openshellGateway")],
)
def test_installation_needs_the_images_without_a_default(given: str, missing: str) -> None:
    result = subprocess.run([*_BASE, *_API_SERVER, "--set", given], capture_output=True, text=True)
    assert result.returncode != 0
    assert missing in result.stderr


def test_control_service_configuration_holds_the_registry(objects: list[dict[str, Any]]) -> None:
    config = _named(objects, "ConfigMap", "heartwood-control")
    configuration = json.loads(config["data"]["configuration.json"])
    assert configuration["audience"] == "heartwood-control"
    assert configuration["keycloak"]["issuer"].endswith("/realms/heartwood")
    assert configuration["service_callers"] == ["system:serviceaccount:heartwood-sessions:hub"]
    sessions = configuration["sessions"]
    assert sessions["openshell_gateway_image"].startswith("registry.example.test/gateway@sha256:")
    assert sessions["control_namespace"] == "heartwood-platform"
    assert sessions["gateway_namespace"] == "heartwood-sessions"
    assert sessions["quota"]["pods"] == "20"
    rendered = _named(objects, "ConfigMap", "heartwood-control-openshell")
    committed = (_CHART / "files" / "openshell-session.json").read_text(encoding="utf-8")
    assert json.loads(rendered["data"]["openshell-session.json"]) == json.loads(committed)
    ingress = _named(objects, "NetworkPolicy", "heartwood-control")["spec"]["ingress"]
    assert ingress == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "heartwood-sessions"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }
    ]


def test_installation_needs_the_api_server_addresses() -> None:
    command = [*_BASE, "--set", _SERVICES, "--set", _GATEWAY]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "sessions.apiServer is empty" in result.stderr
