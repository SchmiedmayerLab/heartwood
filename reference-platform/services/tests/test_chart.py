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
from fakes import SYNTHETIC_PRICES
from heartwood_platform.control.__main__ import Settings

_CHART = Path(__file__).resolve().parents[2] / "chart" / "heartwood-platform"
_DIGEST = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_TEMPLATE = ["helm", "template", "heartwood-platform", str(_CHART)]
_TEMPLATE += ["--namespace", "heartwood-platform"]
# The platform services image has no default.
_SERVICES = "images.platformServices=registry.example.test/services@sha256:" + "0" * 64
_GATEWAY = "images.openshellGateway=registry.example.test/gateway@sha256:" + "1" * 64
_HOSTED = "images.hostedGateway=registry.example.test/hosted-gateway@sha256:" + "4" * 64
_WORKER = "hub.worker.image=registry.example.test/worker@sha256:" + "5" * 64
# The model route, which has no default either.
_MODELS = (
    'inference.models=[{"name":"synthetic-coder","maxInputTokens":32768,"maxOutputTokens":4096,'
    '"litellmParams":{"model":"openai/synthetic-coder","api_base":"https://models.example.test/v1"}}]'
)
# The worker's interpreter, which has no default either.
_BINARIES = (
    "hub.modelRoute.binaries="
    '["/opt/heartwood/python/cpython-3.12.15-linux-x86_64-gnu/bin/python3.12"]'
)
_IMAGES = ["--set", _SERVICES, "--set", _GATEWAY, "--set", _HOSTED, "--set", _WORKER]
_RUNTIME = "images.modelRuntime=registry.example.test/runtime@sha256:" + "6" * 64
_IMAGES += ["--set-json", _MODELS, "--set-json", _BINARIES, "--set", _RUNTIME]
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
        # The control service and the Hub use their ServiceAccount tokens.
        assert spec["automountServiceAccountToken"] is (name in {"heartwood-control", "hub"}), name
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
    assert {role["metadata"]["name"] for role in roles} == _FIXED_ROLES | {
        "heartwood-control",
        "heartwood-hub-spawner",
    }
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


def test_control_service_reads_the_generated_actor_key(objects: list[dict[str, Any]]) -> None:
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    path = Path(configuration["sessions"]["actor_key_file"])
    secret = _named(objects, "Secret", "heartwood-actor-key")
    spec = _named(objects, "StatefulSet", "heartwood-control")["spec"]["template"]["spec"]
    (volume,) = [
        item
        for item in spec["volumes"]
        if item.get("secret", {}).get("secretName") == "heartwood-actor-key"
    ]
    (mount,) = [
        item for item in spec["containers"][0]["volumeMounts"] if item["name"] == volume["name"]
    ]

    assert len(base64.b64decode(secret["data"][path.name])) >= 32
    assert Path(mount["mountPath"]) == path.parent
    assert mount["readOnly"] is True
    # Only the control service mounts the key; gateways receive it in their own Secret.
    assert [
        document["metadata"]["name"]
        for document in objects
        if "heartwood-actor-key" in json.dumps(document.get("spec", {}))
    ] == ["heartwood-control"]


@pytest.mark.parametrize("keycloak", [None, "https://id.example.test"])
def test_the_hub_and_the_control_service_derive_identifiers_from_one_issuer(
    keycloak: str | None,
) -> None:
    extra = [] if keycloak is None else ["--set", f"keycloak.url={keycloak}"]
    rendered = subprocess.run([*_TEMPLATE, *extra], capture_output=True, text=True, check=True)
    objects = [item for item in yaml.safe_load_all(rendered.stdout) if item]
    hub = json.loads(_named(objects, "ConfigMap", "heartwood-hub")["data"]["settings.json"])
    control = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )

    assert hub["issuer"] == control["keycloak"]["issuer"]
    assert keycloak is None or hub["issuer"] == f"{keycloak}/realms/heartwood"


def test_researchers_are_named_by_identifier_under_a_kept_key(
    objects: list[dict[str, Any]],
) -> None:
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    secret = _named(objects, "Secret", "heartwood-researcher-ids")

    assert configuration["redirect_uri_template"].endswith(
        "/user/{researcher}/{project}/oauth_callback"
    )
    assert len(base64.b64decode(secret["data"]["key"])) >= 32
    assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    for document in ("heartwood-actor-key", "heartwood-researcher-ids"):
        annotations = _named(objects, "Secret", document)["metadata"]["annotations"]
        assert annotations["helm.sh/resource-policy"] == "keep"
    # The control service and the Hub mount the key; the gateways never receive it.
    assert sorted(
        document["metadata"]["name"]
        for document in objects
        if "heartwood-researcher-ids" in json.dumps(document.get("spec", {}))
    ) == ["heartwood-control", "hub"]
    spec = _named(objects, "StatefulSet", "heartwood-control")["spec"]["template"]["spec"]
    (volume,) = [
        item
        for item in spec["volumes"]
        if item.get("secret", {}).get("secretName") == "heartwood-researcher-ids"
    ]
    (mount,) = [
        item for item in spec["containers"][0]["volumeMounts"] if item["name"] == volume["name"]
    ]
    assert Path(configuration["researcher_id_key_file"]).parent == Path(mount["mountPath"])


@pytest.mark.parametrize(
    "name",
    [
        "heartwood-control-scope",
        "heartwood-staging-shape",
        "heartwood-staging-names",
        "heartwood-session-services",
    ],
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
            # The JupyterHub chart's values.yaml key holds its rendered values, which carry
            # no generated credential.
            if key not in {"username", "access-key"}
            and f"{item['metadata']['name']}/{key}" != "hub/values.yaml"
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


def test_control_service_serves_only_https_with_a_certificate_of_the_platform_ca(
    objects: list[dict[str, Any]], tmp_path: Path
) -> None:
    authority = tmp_path / "ca.crt"
    authority.write_bytes(
        base64.b64decode(_named(objects, "Secret", "platform-ca")["data"]["tls.crt"])
    )
    secret = _named(objects, "Secret", "heartwood-control-tls")
    assert secret["type"] == "kubernetes.io/tls"
    serving = tmp_path / "control.crt"
    serving.write_bytes(base64.b64decode(secret["data"]["tls.crt"]))
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
    for name in (
        "heartwood-control",
        "heartwood-control.heartwood-platform",
        "heartwood-control.heartwood-platform.svc",
        "heartwood-control.heartwood-platform.svc.cluster.local",
    ):
        assert f"DNS:{name}," in f"{text.strip()},"

    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    # One mounted CA file serves the trust in Keycloak and the copies for sessions.
    assert configuration["keycloak"]["ca_file"] == configuration["sessions"]["platform_ca_file"]
    spec = _named(objects, "StatefulSet", "heartwood-control")["spec"]["template"]["spec"]
    (volume,) = [item for item in spec["volumes"] if "secret" in item and item["name"] == "tls"]
    assert volume["secret"]["secretName"] == "heartwood-control-tls"
    (container,) = spec["containers"]
    (mount,) = [item for item in container["volumeMounts"] if item["name"] == "tls"]
    tls = configuration["tls"]
    assert {Path(tls["certificate_file"]).parent, Path(tls["key_file"]).parent} == {
        Path(mount["mountPath"])
    }
    assert [port["containerPort"] for port in container["ports"]] == [8443]
    assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    # A reload copies the key into the temporary directory, which stays in memory.
    (temporary,) = [item for item in spec["volumes"] if item["name"] == "tmp"]
    assert temporary["emptyDir"]["medium"] == "Memory"
    service = _named(objects, "Service", "heartwood-control")
    assert [port["port"] for port in service["spec"]["ports"]] == [8443]


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
    assert configuration["service_callers"] == ["system:serviceaccount:heartwood-platform:hub"]
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
                },
                {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": "jupyterhub",
                            "app.kubernetes.io/instance": "heartwood-platform",
                            "app.kubernetes.io/component": "hub",
                        }
                    }
                },
            ],
            "ports": [{"protocol": "TCP", "port": 8443}],
        }
    ]


def test_installation_needs_the_api_server_addresses() -> None:
    command = [*_BASE, "--set", _SERVICES, "--set", _GATEWAY, "--set", _HOSTED, "--set", _WORKER]
    command += ["--set-json", _MODELS, "--set-json", _BINARIES]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "sessions.apiServer is empty" in result.stderr


def test_the_model_route_needs_the_programs_of_the_worker_image() -> None:
    command = [*_BASE, "--set", _SERVICES, "--set", _GATEWAY, "--set", _HOSTED, "--set", _WORKER]
    command += ["--set-json", _MODELS, "--set", _RUNTIME, *_API_SERVER]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "hub.modelRoute.binaries must name the programs of hub.worker.image" in result.stderr
    # The kind values name the program of their stand-in worker image.
    command += ["--values", str(_CHART / "values-kind.yaml")]
    assert subprocess.run(command, capture_output=True, text=True).returncode == 0


def test_control_service_configuration_holds_the_synthetic_prices(
    objects: list[dict[str, Any]],
) -> None:
    config = _named(objects, "ConfigMap", "heartwood-control")
    configuration = json.loads(config["data"]["configuration.json"])

    settings = Settings.model_validate_json(config["data"]["configuration.json"])
    # The tests price with the chart's synthetic prices.
    assert configuration["synthetic_prices"] == SYNTHETIC_PRICES
    assert settings.synthetic_prices.denomination.unit == "USD"


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ("syntheticPrices.skuPrices[0].UnitPrice=0.5", "/syntheticPrices/skuPrices/0/UnitPrice"),
        ('syntheticPrices.skuPrices[0].UnitPrice="-1"', "/syntheticPrices/skuPrices/0/UnitPrice"),
        ('syntheticPrices.skuPrices[0].PricingQuantity="1"', "'PricingQuantity' not allowed"),
        (
            'syntheticPrices.skuPrices[0].SkuPriceEffectiveStart="2026-13-45T00:00:00Z"',
            "/syntheticPrices/skuPrices/0/SkuPriceEffectiveStart",
        ),
        (
            'syntheticPrices.runtimeProfiles[0].kind="job"',
            "/syntheticPrices/runtimeProfiles/0/kind",
        ),
    ],
)
def test_schema_rejects_inconsistent_synthetic_prices(value: str, path: str) -> None:
    result = subprocess.run([*_TEMPLATE, "--set-json", value], capture_output=True, text=True)
    assert result.returncode != 0
    assert path in result.stderr


def test_the_operator_account_may_only_rotate_client_secrets(
    objects: list[dict[str, Any]],
) -> None:
    account = _named(objects, "ServiceAccount", "heartwood-operator")
    assert account["automountServiceAccountToken"] is False
    bound = [
        subject
        for item in objects
        if item["kind"] in {"RoleBinding", "ClusterRoleBinding"}
        for subject in item.get("subjects", [])
    ]
    assert not [subject for subject in bound if subject["name"] == "heartwood-operator"]
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    operator = "system:serviceaccount:heartwood-platform:heartwood-operator"
    assert configuration["operator_callers"] == [operator]
    assert operator not in configuration["service_callers"]


def test_staging_names_are_left_to_the_gateway_accounts_of_the_sessions_namespace(
    objects: list[dict[str, Any]],
) -> None:
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-staging-names")["spec"]
    selector = policy["matchConstraints"]["namespaceSelector"]
    assert selector == {
        "matchExpressions": [{"key": "heartwood.platform/session", "operator": "Exists"}]
    }
    assert policy["matchConstraints"]["resourceRules"] == [
        {"apiGroups": [""], "apiVersions": ["*"], "operations": ["CREATE"], "resources": ["pods"]},
        {
            "apiGroups": ["agents.x-k8s.io"],
            "apiVersions": ["*"],
            "operations": ["CREATE"],
            "resources": ["sandboxes"],
        },
    ]
    assert [condition["name"] for condition in policy["matchConditions"]] == [
        "service-account",
        "staging-name",
    ]
    (rule,) = policy["validations"]
    assert rule["expression"] == (
        "request.userInfo.username.startsWith('system:serviceaccount:heartwood-sessions:hw-gw-')"
    )


@pytest.mark.parametrize(
    ("member", "accepted"),
    [
        ("auth0|64ab12cd34ef56ab78cd90ef", True),
        ("synthetic@example.test", True),
        ("109876543210987654321", True),
        (" padded", False),
        ("padded\u3000", False),
        ("zero\u200bwidth", False),
        ("a\tb", False),
        ("line\nbreak", False),
        ("s" * 256, False),
    ],
)
def test_project_members_are_subjects_in_any_provider_form(member: str, accepted: bool) -> None:
    project = {"id": "demo", "billingAttributionRef": "g", "policyRef": "p", "members": [member]}
    result = subprocess.run(
        [*_TEMPLATE, "--set-json", f"projects={json.dumps([project])}"],
        capture_output=True,
        text=True,
    )

    assert (result.returncode == 0) is accepted, result.stderr


def test_control_service_configuration_holds_the_budgets() -> None:
    config = _named(_render("values-kind.yaml"), "ConfigMap", "heartwood-control")

    settings = Settings.model_validate_json(config["data"]["configuration.json"])

    budgets = {project.id: project.budgets for project in settings.projects}
    assert budgets == {"demo": {"inference": "5", "compute": "50"}, "other": {"inference": "0.006"}}
    assert settings.inference.session_reservation == "1"


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ('projects[0].budgets.inference="-1"', "/projects/0/budgets/inference"),
        ('projects[0].budgets.storage="1"', "'storage' not allowed"),
        ("control.inference.sessionReservation=1", "/control/inference/sessionReservation"),
    ],
)
def test_schema_rejects_invalid_budgets(value: str, path: str) -> None:
    result = subprocess.run([*_TEMPLATE, "--set-json", value], capture_output=True, text=True)
    assert result.returncode != 0
    assert path in result.stderr


def _litellm(objects: list[dict[str, Any]]) -> dict[str, Any]:
    config = yaml.safe_load(_named(objects, "ConfigMap", "litellm")["data"]["config.yaml"])
    assert isinstance(config, dict)
    return config


def test_litellm_records_costs_from_the_price_sheet_and_trusts_the_platform_ca() -> None:
    objects = _render("values-kind.yaml")
    config = _litellm(objects)

    (model,) = config["model_list"]
    assert model["model_name"] == "synthetic-coder"
    assert model["model_info"]["input_cost_per_token"] == pytest.approx(0.5 / 1_000_000)
    assert model["model_info"]["output_cost_per_token"] == pytest.approx(1.5 / 1_000_000)
    # A model without apiKeySecret gets no API key.
    assert "api_key" not in model["litellm_params"]
    assert config["litellm_settings"]["ssl_verify"] == "/etc/litellm/trust/ca.crt"
    assert config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    deployment = _named(objects, "Deployment", "litellm")["spec"]["template"]["spec"]
    (trust,) = deployment["initContainers"]
    assert trust["command"][-1] == "cat /etc/litellm/platform-ca/ca.crt > /etc/litellm/trust/ca.crt"
    (container,) = deployment["containers"]
    assert "--ssl_certfile_path" in container["args"]
    assert container["image"].startswith("ghcr.io/berriai/litellm-non_root:v1.110.0@sha256:")
    assert deployment["securityContext"]["runAsUser"] == 65534
    # The control service holds the master key; LiteLLM's pod mounts no service token.
    assert deployment["automountServiceAccountToken"] is False
    control = _named(objects, "StatefulSet", "heartwood-control")["spec"]["template"]["spec"]
    assert {
        "name": "litellm",
        "secret": {"secretName": "litellm", "items": [{"key": "master-key", "path": "master-key"}]},
    } in control["volumes"]


def test_litellm_accepts_only_the_control_service_supervisors_and_gateways() -> None:
    objects = _render("values-kind.yaml")
    (rule,) = _named(objects, "NetworkPolicy", "litellm")["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 4000}]
    sources = rule["from"]
    assert sources[0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "control"
    assert sources[1] == {
        "namespaceSelector": {
            "matchExpressions": [{"key": "heartwood.platform/session", "operator": "Exists"}]
        },
        "podSelector": {"matchLabels": {"openshell.ai/boundary-role": "supervisor"}},
    }
    assert sources[2]["podSelector"] == {
        "matchLabels": {"heartwood.platform/role": "hosted-gateway"}
    }
    (synthetic,) = _named(objects, "NetworkPolicy", "synthetic-model")["spec"]["ingress"]
    assert synthetic["from"] == [
        {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "heartwood-platform",
                    "app.kubernetes.io/instance": "heartwood-platform",
                    "app.kubernetes.io/component": "litellm",
                }
            }
        }
    ]


def test_the_hosted_model_example_renders_a_route_to_hosted_models() -> None:
    readme = (_CHART.parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Model Route", 1)[1]
    example = yaml.safe_load(section.split("```yaml\n", 1)[1].split("```", 1)[0])
    names = [model["name"] for model in example["inference"]["models"]]
    rates = [
        ("input_tokens", "synthetic-coder-input-list"),
        ("output_tokens", "synthetic-coder-output-list"),
    ]
    profiles = [
        {
            "runtimeProfileRef": name,
            "kind": "inference",
            "rates": [
                {"measure": measure, "skuPriceId": price, "quantityPerPricingUnit": "1000000"}
                for measure, price in rates
            ],
        }
        for name in names
    ]
    values = {**example, "syntheticPrices": {"runtimeProfiles": profiles}}
    output = subprocess.run(
        [
            *_TEMPLATE,
            "--set-json",
            f"inference={json.dumps(example['inference'])}",
            "--set-json",
            f"syntheticPrices.runtimeProfiles={json.dumps(profiles)}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    del values
    objects = [item for item in yaml.safe_load_all(output) if item is not None]

    config = _litellm(objects)
    models = {model["model_name"]: model for model in config["model_list"]}
    assert models["hosted-gemini"]["litellm_params"]["model"] == "vertex_ai/gemini-2.5-pro"
    assert models["hosted-gemini"]["litellm_params"]["vertex_credentials"] == (
        "os.environ/VERTEX_CREDENTIALS"
    )
    assert models["hosted-claude"]["litellm_params"]["aws_region_name"] == "us-east-1"
    container = _named(objects, "Deployment", "litellm")["spec"]["template"]["spec"]
    environment = {item["name"]: item for item in container["containers"][0]["env"]}
    assert environment["VERTEX_CREDENTIALS"]["valueFrom"]["secretKeyRef"] == {
        "name": "vertex-credentials",
        "key": "credentials.json",
    }
    (trust,) = container["initContainers"]
    assert "/etc/ssl/certs/ca-certificates.crt" in trust["command"][-1]
    # The control service offers the hosted models to the sessions' keys.
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    assert [model["name"] for model in configuration["inference"]["models"]] == names
    assert configuration["hosted"]["model_route"]["model"] == "hosted-gemini"


@pytest.mark.parametrize(
    ("value", "path"),
    [
        (
            'inference.models=[{"name":"unpriced","maxInputTokens":1,"maxOutputTokens":1,'
            '"litellmParams":{"model":"openai/x"}}]',
            "syntheticPrices has no inference runtime profile unpriced",
        ),
        (
            'inference.models=[{"name":"synthetic-coder","maxInputTokens":1,"maxOutputTokens":1,'
            '"litellmParams":{"model":"openai/x","api_key":"sk-in-values"}}]',
            "/inference/models/0/litellmParams",
        ),
        (
            'inference.environment=[{"name":"LITELLM_MASTER_KEY",'
            '"secretKeyRef":{"name":"a","key":"b"}}]',
            "/inference/environment/0/name",
        ),
        ("inference.models=[]", "inference.models must name at least one model"),
    ],
)
def test_the_model_route_refuses_unpriced_models_and_credentials_in_values(
    value: str, path: str
) -> None:
    result = subprocess.run([*_TEMPLATE, "--set-json", value], capture_output=True, text=True)
    assert result.returncode != 0
    assert path in result.stderr


def _render_kind_route() -> list[dict[str, Any]]:
    """Render the kind values with their own models of the route."""
    images = [*_IMAGES[: _IMAGES.index("--set-json")], "--set", _RUNTIME, *_API_SERVER]
    output = subprocess.run(
        [*_BASE, *images, "--values", str(_CHART / "values-kind.yaml")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(output) if item is not None]


def test_the_local_model_serves_tls_with_a_key_file_and_no_web_interface() -> None:
    objects = _render_kind_route()
    spec = _named(objects, "Deployment", "llama-server")["spec"]["template"]["spec"]
    (model,) = spec["initContainers"]
    (server,) = spec["containers"]
    arguments = server["args"]

    assert server["image"] == _RUNTIME.partition("=")[2]
    assert server["command"] == ["/opt/llama.cpp/llama-server"]
    for flag, value in (
        ("--ssl-key-file", "/etc/llama-server/tls/tls.key"),
        ("--ssl-cert-file", "/etc/llama-server/tls/tls.crt"),
        ("--api-key-file", "/etc/llama-server/key/api-key"),
        ("--alias", "local-model"),
    ):
        assert arguments[arguments.index(flag) + 1] == value
    assert "--no-webui" in arguments
    assert "--api-key" not in arguments
    assert not any(item.get("valueFrom") for item in server.get("env", []))
    assert {"name": "tls", "secret": {"secretName": "llama-server-tls"}} in spec["volumes"]
    # The weights are downloaded over HTTPS and checked against their digest.
    environment = {item["name"]: item["value"] for item in model["env"]}
    assert environment["MODEL_URL"].startswith("https://huggingface.co/ggml-org/")
    assert environment["MODEL_SHA256"] == (
        "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d"
    )
    assert "sha256sum --check" in model["command"][-1]
    (rule,) = _named(objects, "NetworkPolicy", "llama-server")["spec"]["ingress"]
    assert [
        source["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
        for source in rule["from"]
    ] == ["litellm"]
    assert "llama-server-tls" in {
        item["metadata"]["name"] for item in objects if item["kind"] == "Secret"
    }
    config = _litellm(objects)
    models = {item["model_name"]: item for item in config["model_list"]}
    assert models["local-model"]["litellm_params"] == {
        "model": "openai/local-model",
        "api_base": "https://llama-server.heartwood-platform.svc.cluster.local:8443/v1",
        "api_key": "os.environ/HW_MODEL_KEY_1",
    }
    assert models["local-model"]["model_info"]["input_cost_per_token"] == pytest.approx(1e-7)
    litellm = _named(objects, "Deployment", "litellm")["spec"]["template"]["spec"]
    environment_names = {item["name"]: item for item in litellm["containers"][0]["env"]}
    assert environment_names["HW_MODEL_KEY_1"]["valueFrom"]["secretKeyRef"] == {
        "name": "llama-server",
        "key": "api-key",
    }


def test_the_local_model_needs_the_runtime_image_and_its_weights() -> None:
    images = [*_IMAGES[: _IMAGES.index("--set-json")], *_API_SERVER]
    kind = ["--values", str(_CHART / "values-kind.yaml")]
    result = subprocess.run([*_BASE, *images, *kind], capture_output=True, text=True)
    assert result.returncode != 0
    assert "images.modelRuntime must name the runtime image by digest" in result.stderr
    result = subprocess.run(
        [*_BASE, *images, *kind, "--set", _RUNTIME, "--set", "inference.localModel.model.url="],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "inference.localModel.model.url is required" in result.stderr
