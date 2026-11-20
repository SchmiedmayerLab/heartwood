# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The JupyterHub of the chart: its pinned chart, permissions, admission and hooks."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from types import ModuleType
from typing import Any

import pytest
import yaml
from test_chart import _CHART, _TEMPLATE, _named

# The digest of jupyterhub-4.4.5.tgz in https://hub.jupyter.org/helm-chart/index.yaml.
_JUPYTERHUB_DIGEST = "27ad182c708244537f425a7ec3354d1a098776e7acd96b7f0d9a4d38f0cda415"
_SESSION = "0123456789abcdef"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "heartwood_hub", _CHART / "files" / "hub" / "heartwood_hub.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def objects() -> list[dict[str, Any]]:
    output = subprocess.run(
        [*_TEMPLATE, "--values", str(_CHART / "values-kind.yaml")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(output) if item is not None]


def test_the_jupyterhub_chart_is_the_pinned_archive() -> None:
    archive = _CHART / "charts" / "jupyterhub-4.4.5.tgz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == _JUPYTERHUB_DIGEST
    lock = yaml.safe_load((_CHART / "Chart.lock").read_text(encoding="utf-8"))
    assert lock["dependencies"] == [
        {
            "name": "jupyterhub",
            "repository": "https://hub.jupyter.org/helm-chart/",
            "version": "4.4.5",
        }
    ]


def test_the_hub_may_spawn_in_the_sessions_namespace_and_read_no_secret(
    objects: list[dict[str, Any]],
) -> None:
    role = _named(objects, "Role", "heartwood-hub-spawner")
    assert role["metadata"]["namespace"] == "heartwood-sessions"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "delete", "list", "watch"]},
        {
            "apiGroups": [""],
            "resources": ["persistentvolumeclaims"],
            "verbs": ["create", "delete", "get"],
        },
        {"apiGroups": [""], "resources": ["events"], "verbs": ["list", "watch"]},
    ]
    binding = _named(objects, "RoleBinding", "heartwood-hub-spawner")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "hub", "namespace": "heartwood-platform"}
    ]
    # The JupyterHub chart adds no role of its own.
    names = {item["metadata"]["name"] for item in objects if item["kind"].endswith("Role")}
    assert names.isdisjoint({"hub", "user-scheduler", "hub-user-scheduler", "autohttps"})
    assert not [item for item in objects if item["kind"] == "DaemonSet"]


def test_hub_pods_are_admitted_only_in_the_gateway_shape(objects: list[dict[str, Any]]) -> None:
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-hub-pods")
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert policy["spec"]["matchConditions"] == [
        {
            "name": "hub",
            "expression": "request.userInfo.username == "
            "'system:serviceaccount:heartwood-platform:hub'",
        }
    ]
    validations = " ".join(item["expression"] for item in policy["spec"]["validations"])
    assert "registry.example.test/hosted-gateway@sha256:" + "4" * 64 in validations
    assert "'hw-gw-' + variables.session" in validations
    assert "'hw-home-' + variables.session" in validations
    assert "/usr/local/libexec/heartwood-hosted-configure" in validations
    binding = _named(objects, "ValidatingAdmissionPolicyBinding", "heartwood-hub-pods")
    assert binding["spec"]["validationActions"] == ["Deny"]


def test_the_arguments_of_the_hook_match_the_admission_policy(
    objects: list[dict[str, Any]],
) -> None:
    hub = _module()
    settings = json.loads(_named(objects, "ConfigMap", "heartwood-hub")["data"]["settings.json"])
    arguments = ["heartwood", "gateway", "serve"]
    arguments += hub.gateway_arguments(settings, "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001", "demo")
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-hub-pods")
    expression = policy["spec"]["validations"][3]["expression"]
    assert f"size(object.spec.containers[0].args) == {len(arguments)}" in expression
    for index, value in enumerate(arguments):
        if index != 14:
            assert f"args[{index}] == {json.dumps(value)}" in expression or (
                f"args[{index}] == '{value}'" in expression
            )
    assert arguments[14] == "/user/6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001/demo"


def test_the_hook_mounts_the_secret_only_in_the_init_container() -> None:
    pod = _module().pod_settings(
        {"session": _SESSION, "gateway_service_account": f"hw-gw-{_SESSION}"}
    )
    assert pod["service_account"] == f"hw-gw-{_SESSION}"
    assert pod["pvc_name"] == f"hw-home-{_SESSION}"
    assert pod["labels"] == {
        "heartwood.platform/session": _SESSION,
        "heartwood.platform/role": "hosted-gateway",
    }
    (secret,) = [volume for volume in pod["volumes"] if "secret" in volume]
    assert secret["secret"]["secretName"] == f"hw-gw-{_SESSION}"
    assert secret["name"] in {mount["name"] for mount in pod["init_mounts"]}
    assert secret["name"] not in {mount["name"] for mount in pod["volume_mounts"]}
    (work,) = [volume for volume in pod["volumes"] if volume["name"] == "work"]
    assert work["emptyDir"]["medium"] == "Memory"


def test_a_named_server_offers_only_its_own_project() -> None:
    profiles = _module().profiles
    assert profiles(["demo", "other"], "demo") == [
        {"display_name": "demo", "slug": "demo", "default": True}
    ]
    assert profiles(["demo"], "other") == []
    assert profiles(["demo"], "") == []


def test_the_hub_signs_in_through_keycloak_and_calls_the_control_service_over_tls(
    objects: list[dict[str, Any]],
) -> None:
    settings = json.loads(_named(objects, "ConfigMap", "heartwood-hub")["data"]["settings.json"])
    assert settings["issuer"].startswith("https://keycloak.")
    assert settings["control_url"].startswith("https://heartwood-control.")
    realm = json.loads(
        _named(objects, "ConfigMap", "keycloak-realm")["data"]["heartwood-realm.json"]
    )
    (client,) = [item for item in realm["clients"] if item["clientId"] == "heartwood-hub"]
    assert client["secret"] == "${HEARTWOOD_HUB_CLIENT_SECRET}"
    assert client["redirectUris"] == [f"{settings['public_origin']}/hub/oauth_callback"]
    assert client["directAccessGrantsEnabled"] is False
    hub = _named(objects, "Deployment", "hub")["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in hub["volumes"]}
    (source,) = volumes["control-token"]["projected"]["sources"]
    assert source["serviceAccountToken"]["audience"] == "heartwood-control"
    assert volumes["platform-ca"]["configMap"]["name"] == "platform-ca"
    mounts = {mount["name"]: mount["mountPath"] for mount in hub["containers"][0]["volumeMounts"]}
    assert settings["ca_file"].startswith(mounts["platform-ca"])
    assert settings["token_file"].startswith(mounts["control-token"])


def test_only_the_hub_and_the_proxy_reach_the_gateway_pods(objects: list[dict[str, Any]]) -> None:
    policy = _named(objects, "NetworkPolicy", "hosted-gateways")
    assert policy["metadata"]["namespace"] == "heartwood-sessions"
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8888}]
    (source,) = rule["from"]
    assert source["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "heartwood-platform"}
    }
    namespace = _named(objects, "Namespace", "heartwood-sessions")
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
