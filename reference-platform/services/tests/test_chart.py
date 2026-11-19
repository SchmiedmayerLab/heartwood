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


def test_openshell_sandbox_role_creates_and_reads_config_maps_only(
    objects: list[dict[str, Any]],
) -> None:
    rules = _named(objects, "ClusterRole", "heartwood-openshell-sandbox")["rules"]
    (rule,) = [rule for rule in rules if "configmaps" in rule["resources"]]
    assert rule == {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "create"]}


def test_session_gateway_role_grants_what_the_worker_adapter_uses(
    objects: list[dict[str, Any]],
) -> None:
    # The adapter reads objects one request at a time and deletes without waiting, so it
    # never watches.
    rules = _named(objects, "ClusterRole", "heartwood-session-gateway")["rules"]
    assert rules == [
        {
            "apiGroups": [""],
            "resources": ["pods", "persistentvolumeclaims"],
            "verbs": ["create", "delete", "get", "list"],
        },
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "verbs": ["get", "list"],
        },
        {"apiGroups": ["agents.x-k8s.io"], "resources": ["sandboxes"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get"]},
    ]


def test_control_service_reads_the_platform_ca_certificate(objects: list[dict[str, Any]]) -> None:
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    path = Path(configuration["sessions"]["platform_ca_file"])
    spec = _named(objects, "StatefulSet", "heartwood-control")["spec"]["template"]["spec"]
    (volume,) = [
        item for item in spec["volumes"] if item.get("configMap", {}).get("name") == "platform-ca"
    ]
    (mount,) = [
        item for item in spec["containers"][0]["volumeMounts"] if item["name"] == volume["name"]
    ]

    assert volume["configMap"]["items"] == [{"key": "ca.crt", "path": path.name}]
    assert Path(mount["mountPath"]) == path.parent
    assert mount["readOnly"] is True
    assert "ca.crt" in _named(objects, "ConfigMap", "platform-ca")["data"]


@pytest.mark.parametrize(
    "name", ["heartwood-control-scope", "heartwood-staging-shape", "heartwood-session-services"]
)
def test_admission_policies_fail_closed_and_deny(objects: list[dict[str, Any]], name: str) -> None:
    assert _named(objects, "ValidatingAdmissionPolicy", name)["spec"]["failurePolicy"] == "Fail"
    binding = _named(objects, "ValidatingAdmissionPolicyBinding", name)
    assert binding["spec"] == {"policyName": name, "validationActions": ["Deny"]}


@pytest.mark.parametrize(
    ("images", "listed"),
    [
        ([], "[]"),
        (
            ["registry.example.test/worker@sha256:" + "3" * 64],
            '["registry.example.test/worker@sha256:' + "3" * 64 + '"]',
        ),
        (
            [
                "registry.example.test/worker@sha256:" + "3" * 64,
                "registry.example.test/worker@sha256:" + "4" * 64,
            ],
            '["registry.example.test/worker@sha256:'
            + "3" * 64
            + '","registry.example.test/worker@sha256:'
            + "4" * 64
            + '"]',
        ),
    ],
)
def test_staging_pods_run_only_the_listed_images(images: list[str], listed: str) -> None:
    result = subprocess.run(
        [*_TEMPLATE, "--set-json", f"sessions.stagingImages={json.dumps(images)}"],
        capture_output=True,
        text=True,
        check=True,
    )
    objects = [item for item in yaml.safe_load_all(result.stdout) if item is not None]
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-staging-shape")
    (rule,) = [
        item["expression"]
        for item in policy["spec"]["validations"]
        if item["message"] == "a staging pod runs only a staging image"
    ]
    assert rule == f"!variables.pod || variables.containers.all(c, c.image in {listed})"


@pytest.mark.parametrize("image", ["registry.example.test/worker:latest", ""])
def test_staging_images_are_pinned_by_digest(image: str) -> None:
    result = subprocess.run(
        [*_TEMPLATE, "--set-json", f"sessions.stagingImages={json.dumps([image])}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "/sessions/stagingImages/0" in result.stderr


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
    account, *researchers = realm["users"]
    assert account["clientRoles"] == {"realm-management": ["manage-clients"]}
    # Each password is a reference to the environment of Keycloak, never a value.
    assert [user["credentials"] for user in researchers] == [
        [
            {
                "type": "password",
                "temporary": False,
                "value": f"${{HEARTWOOD_RESEARCHER_PASSWORD_{index}}}",
            }
        ]
        for index in range(len(researchers))
    ]
    assert all("clientRoles" not in user and "realmRoles" not in user for user in researchers)
    passwords = _named(objects, "Secret", "keycloak-researchers")["data"] or {}
    assert set(passwords) == {user["username"] for user in researchers}
    environment = {
        variable["name"]: variable["valueFrom"]["secretKeyRef"]
        for variable in _named(objects, "Deployment", "keycloak")["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
        if variable["name"].startswith("HEARTWOOD_RESEARCHER_PASSWORD_")
    }
    assert environment == {
        f"HEARTWOOD_RESEARCHER_PASSWORD_{index}": {
            "name": "keycloak-researchers",
            "key": user["username"],
        }
        for index, user in enumerate(researchers)
    }
    projects = {scope for scope in scopes if scope.startswith("project-")}
    assert set(clients["heartwood-cli"]["optionalClientScopes"]) == projects


def test_synthetic_researchers_keep_the_subjects_of_the_registry() -> None:
    objects = _render("values-kind.yaml")
    realm = json.loads(
        _named(objects, "ConfigMap", "keycloak-realm")["data"]["heartwood-realm.json"]
    )
    subjects = {user["id"] for user in realm["users"] if "id" in user}
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    members = {member for project in configuration["projects"] for member in project["members"]}
    assert subjects == members
    assert len(subjects) == 2


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
    published = [
        item["data"]["ca.crt"]
        for item in objects
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "platform-ca"
    ]
    assert len(published) == 1
    for text in published:
        assert text.strip() == certificate.decode().strip()
        assert "PRIVATE KEY" not in text


def test_keycloak_serves_only_https_with_a_certificate_of_the_platform_ca(
    objects: list[dict[str, Any]], tmp_path: Path
) -> None:
    authority = tmp_path / "ca.crt"
    authority.write_bytes(
        base64.b64decode(_named(objects, "Secret", "platform-ca")["data"]["tls.crt"])
    )
    serving = tmp_path / "keycloak.crt"
    serving.write_bytes(
        base64.b64decode(_named(objects, "Secret", "keycloak-tls")["data"]["tls.crt"])
    )
    subprocess.run(
        ["openssl", "verify", "-CAfile", str(authority), str(serving)],
        check=True,
        capture_output=True,
    )
    text = subprocess.run(
        ["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(serving)],
        check=True,
        capture_output=True,
    ).stdout.decode()
    for name in ("keycloak", "keycloak.heartwood-platform.svc.cluster.local"):
        assert f"DNS:{name}," in f"{text.strip()},"
    container = _named(objects, "Deployment", "keycloak")["spec"]["template"]["spec"]["containers"][
        0
    ]
    variables = {variable["name"]: variable.get("value") for variable in container["env"]}
    assert variables["KC_HTTP_ENABLED"] == "false"
    assert variables["KC_HTTPS_CERTIFICATES_RELOAD_PERIOD"] == "1m"
    assert variables["KC_HTTPS_CERTIFICATE_FILE"] == "/opt/keycloak/conf/tls/tls.crt"
    assert variables["KC_HOSTNAME"].startswith("https://")
    assert [port["containerPort"] for port in container["ports"]] == [8443, 9000]
    for probe in ("startupProbe", "readinessProbe"):
        assert container[probe]["httpGet"]["scheme"] == "HTTPS"
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    assert configuration["keycloak"]["url"] == "https://keycloak:8443"
    assert configuration["keycloak"]["issuer"].startswith("https://")


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


@pytest.mark.parametrize(
    ("repository", "accepted"),
    [
        ("", True),
        ("kind-registry:5000/openshell", True),
        ("registry.example.test/mirror/openshell", True),
        ("registry.example.test/openshell/", False),
        ("registry.example.test/openshell:0.1.9", False),
        ("registry.example.test/openshell@sha256:" + "2" * 64, False),
    ],
)
def test_sandbox_image_repository_is_passed_to_the_control_service(
    repository: str, accepted: bool
) -> None:
    result = subprocess.run(
        [*_TEMPLATE, "--set", f"images.openshellRepository={repository}"],
        capture_output=True,
        text=True,
    )
    if not accepted:
        assert result.returncode != 0
        assert "/images/openshellRepository" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    objects = [item for item in yaml.safe_load_all(result.stdout) if item is not None]
    config = _named(objects, "ConfigMap", "heartwood-control")
    sessions = json.loads(config["data"]["configuration.json"])["sessions"]
    assert sessions["openshell_repository"] == repository


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
