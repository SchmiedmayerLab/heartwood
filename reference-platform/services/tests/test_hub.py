# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The JupyterHub of the chart: its pinned chart, permissions, admission and hooks."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from heartwood_platform.control.researchers import RESEARCHER_ID_PATTERN, researcher_id
from test_chart import _CHART, _TEMPLATE, _named

# The digest of jupyterhub-4.4.5.tgz in https://hub.jupyter.org/helm-chart/index.yaml.
_JUPYTERHUB_DIGEST = "27ad182c708244537f425a7ec3354d1a098776e7acd96b7f0d9a4d38f0cda415"
_SESSION = "0123456789abcdef"
_RESEARCHER = "r0123456789abcdef0123456789abcdef"


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
    messages = [item["message"] for item in policy["spec"]["validations"]]
    for subject in (
        "ServiceAccount hw-gw-<session>",
        "only the labels",
        "hosted gateway image",
        "configuration script",
        "fixed arguments",
        "default termination message",
        "only annotations of JupyterHub",
        "cluster's DNS",
        "without host access",
        "its volume claim hw-home-<session>",
        "exactly the volumes and paths",
        "name no data source",
    ):
        assert [message for message in messages if subject in message], subject
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
    arguments += hub.gateway_arguments(settings, _RESEARCHER, "demo")
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-hub-pods")
    (expression,) = [
        item["expression"]
        for item in policy["spec"]["validations"]
        if "fixed arguments" in item["message"]
    ]
    assert f"size(object.spec.containers[0].args) == {len(arguments)}" in expression
    for index, value in enumerate(arguments):
        if index != 14:
            assert f"args[{index}] == {json.dumps(value)}" in expression or (
                f"args[{index}] == '{value}'" in expression
            )
    assert arguments[14] == f"/user/{_RESEARCHER}/demo"
    # The policy admits a base path of the researcher identifier form alone.
    assert "'^/user/r[0-9a-f]{32}/" in expression


def test_the_hook_mounts_the_secret_only_in_the_init_container(
    objects: list[dict[str, Any]],
) -> None:
    settings = json.loads(_named(objects, "ConfigMap", "heartwood-hub")["data"]["settings.json"])
    pod = _module().pod_settings(
        {"session": _SESSION, "gateway_service_account": f"hw-gw-{_SESSION}"}, settings
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
    # The policy admits exactly the mounts of the chart's settings.
    expression = " ".join(
        item["expression"]
        for item in _named(objects, "ValidatingAdmissionPolicy", "heartwood-hub-pods")["spec"][
            "validations"
        ]
    )
    for mount in pod["volume_mounts"] + pod["init_mounts"]:
        assert f"m.mountPath == {json.dumps(mount['mountPath'])}" in expression
    assert "size(object.spec.containers[0].volumeMounts.filter" in expression
    assert "terminationMessagePath" in expression
    assert "hostAliases" in expression


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
    # The Hub reads the researcher identifier key from the control service's Secret.
    assert volumes["researcher-ids"]["secret"]["secretName"] == "heartwood-researcher-ids"
    assert settings["researcher_id_key_file"].startswith(mounts["researcher-ids"])


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


def test_only_project_server_names_reach_the_control_service() -> None:
    launch_path = _module().launch_path
    expected = f"/v1/researchers/{_RESEARCHER}/projects/demo/launch"
    assert launch_path(_RESEARCHER, "demo") == expected
    for name in ("x/../../../other/projects/demo", "..", "demo/x", "Demo", "", "demo%2F"):
        assert launch_path(_RESEARCHER, name) is None, name
    # Only user names of the researcher identifier form reach a path, never a subject.
    for user in ("../hub", "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001", _RESEARCHER.upper(), "r"):
        assert launch_path(user, "demo") is None, user


def test_the_hub_derives_the_control_services_researcher_identifier() -> None:
    hub = _module()
    issuer = "https://keycloak.example.test/realms/heartwood"
    key = b"synthetic-researcher-id-key-0123456789"

    for subject in ("6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001", "auth0|64ab", "a@example.test"):
        identifier = hub.researcher_id(issuer, subject, key)
        assert identifier == researcher_id(issuer, subject, key)
        assert re.fullmatch(RESEARCHER_ID_PATTERN, identifier)
        assert hub.launch_path(identifier, "demo") is not None
    assert hub.researcher_id(issuer, "a", key) != hub.researcher_id(f"{issuer}/x", "a", key)
    assert hub.researcher_id(issuer, "a", key) != hub.researcher_id(issuer, "a", b"k" * 32)


class _Config:
    """Attribute sections as the traitlets configuration object offers them."""

    def __init__(self) -> None:
        self.__dict__["sections"] = {}

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return self.sections.setdefault(name, _Section())


class _Section:
    pass


@dataclass
class _Hub:
    """The configured Hub with fake JupyterHub, KubeSpawner and tornado modules."""

    config: _Config
    settings: dict[str, Any]
    sent: list[tuple[str, str]]
    deleted: list[str]
    proxy_requests: list[tuple[str, Any]]
    http_error: type[Exception]


@pytest.fixture
def hub(objects: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Hub:
    import sys
    import types

    sent: list[tuple[str, str]] = []
    deleted: list[str] = []
    proxy_requests: list[tuple[str, Any]] = []

    class HTTPError(Exception):
        def __init__(self, status: int, message: str = "") -> None:
            super().__init__(status, message)

    class Response:
        code = 204
        body = b""

    class Client:
        def __init__(self, **options: Any) -> None:  # noqa: ANN401
            self.options = options

        async def fetch(self, request: Any, raise_error: bool = True) -> Response:  # noqa: ANN401, ARG002
            sent.append((request.method, request.url))
            return Response()

    class Request:
        def __init__(self, url: str, method: str, **_: Any) -> None:  # noqa: ANN401
            self.url, self.method = url, method

    class KubeSpawner:
        def __init__(self, user: str, name: str) -> None:
            self.user = types.SimpleNamespace(name=user)
            self.name = name

        async def delete_forever(self) -> None:
            deleted.append(self.name)

    class ConfigurableHTTPProxy:
        async def api_request(
            self,
            path: str,
            method: str = "GET",
            body: Any = None,  # noqa: ANN401, ARG002
            client: Any = None,  # noqa: ANN401
        ) -> None:
            proxy_requests.append((f"{method} {path}", client))

    tornado = types.ModuleType("tornado")
    web = types.ModuleType("tornado.web")
    web.HTTPError = HTTPError  # type: ignore[attr-defined]
    httpclient = types.ModuleType("tornado.httpclient")
    httpclient.AsyncHTTPClient = Client  # type: ignore[attr-defined]
    httpclient.HTTPRequest = Request  # type: ignore[attr-defined]
    kubespawner = types.ModuleType("kubespawner")
    kubespawner.KubeSpawner = KubeSpawner  # type: ignore[attr-defined]
    jupyterhub = types.ModuleType("jupyterhub")
    proxy = types.ModuleType("jupyterhub.proxy")
    proxy.ConfigurableHTTPProxy = ConfigurableHTTPProxy  # type: ignore[attr-defined]
    for name, module in (
        ("tornado", tornado),
        ("tornado.web", web),
        ("tornado.httpclient", httpclient),
        ("kubespawner", kubespawner),
        ("jupyterhub", jupyterhub),
        ("jupyterhub.proxy", proxy),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    settings = json.loads(_named(objects, "ConfigMap", "heartwood-hub")["data"]["settings.json"])
    secret, token = tmp_path / "client-secret", tmp_path / "token"
    key = tmp_path / "researcher-ids"
    secret.write_text("synthetic\n", encoding="utf-8")
    token.write_text("synthetic-token\n", encoding="utf-8")
    key.write_text("synthetic-researcher-id-key-0123456789\n", encoding="utf-8")
    settings |= {
        "client_secret_file": str(secret),
        "token_file": str(token),
        "researcher_id_key_file": str(key),
    }
    config = _Config()
    _module().configure(config, settings)
    return _Hub(config, settings, sent, deleted, proxy_requests, HTTPError)


def test_deleting_a_server_calls_the_control_service_only_for_a_project_name(hub: _Hub) -> None:
    import asyncio

    spawner_class = hub.config.JupyterHub.spawner_class
    username = hub.config.GenericOAuthenticator.username_claim
    subject = "auth0|64ab12cd34ef56ab78cd90ef"
    researcher = username({"sub": subject, "email": "synthetic@example.test"})
    assert researcher == researcher_id(
        hub.settings["issuer"], subject, b"synthetic-researcher-id-key-0123456789"
    )
    for user_info in ({}, {"sub": ""}, {"sub": 7}):
        with pytest.raises(hub.http_error) as refused:
            username(user_info)
        assert refused.value.args == (403, "The sign-in names no subject.")

    for name in ("x/../../../other/projects/demo", "Demo", ""):
        asyncio.run(spawner_class(researcher, name).delete_forever())
    asyncio.run(spawner_class(subject, "demo").delete_forever())
    assert hub.sent == []
    asyncio.run(spawner_class(researcher, "demo").delete_forever())
    assert hub.sent == [
        (
            "DELETE",
            f"{hub.settings['control_url']}/v1/researchers/{researcher}/projects/demo/launch",
        )
    ]
    assert hub.deleted == ["x/../../../other/projects/demo", "Demo", "", "demo", "demo"]


def test_the_proxy_serves_https_with_a_certificate_of_the_platform_ca(
    objects: list[dict[str, Any]], tmp_path: Path
) -> None:
    settings = json.loads(_named(objects, "ConfigMap", "heartwood-hub")["data"]["settings.json"])
    origin = urlsplit(settings["public_origin"])
    assert origin.scheme == "https"
    authority = tmp_path / "ca.crt"
    authority.write_bytes(
        base64.b64decode(_named(objects, "Secret", "platform-ca")["data"]["tls.crt"])
    )
    secret = _named(objects, "Secret", "proxy-tls")
    assert secret["type"] == "kubernetes.io/tls"
    serving = tmp_path / "proxy.crt"
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
    names = {item.strip().removeprefix("DNS:") for item in text.splitlines()[-1].split(",")}
    assert names == set(secret["metadata"]["annotations"]["heartwood.platform/names"].split(","))
    assert {
        "proxy-public.heartwood-platform.svc.cluster.local",
        "proxy-api.heartwood-platform.svc.cluster.local",
        origin.hostname,
    } <= names
    # The proxy serves researchers and its API with the certificate, and its Service
    # offers no HTTP port.
    proxy = _named(objects, "Deployment", "proxy")["spec"]["template"]["spec"]
    (container,) = proxy["containers"]
    command = container["command"]
    for argument in (
        "--port=8443",
        "--ssl-key=/etc/chp/tls/tls.key",
        "--ssl-cert=/etc/chp/tls/tls.crt",
        "--api-ssl-key=/etc/chp/tls/tls.key",
        "--api-ssl-cert=/etc/chp/tls/tls.crt",
    ):
        assert argument in command
    (volume,) = proxy["volumes"]
    assert volume["secret"]["secretName"] == "proxy-tls"
    service = _named(objects, "Service", "proxy-public")
    assert service["spec"]["ports"] == [{"name": "https", "port": 443, "targetPort": "https"}]
    assert (
        settings["proxy_api_url"] == "https://proxy-api.heartwood-platform.svc.cluster.local:8001"
    )
    # Researchers reach only the HTTPS port; the HTTP port only redirects.
    policy = _named(objects, "NetworkPolicy", "hub-proxy")
    public, api = policy["spec"]["ingress"]
    assert public == {"ports": [{"protocol": "TCP", "port": 8443}]}
    assert api["ports"] == [{"protocol": "TCP", "port": 8001}]


def test_the_public_origin_must_be_https() -> None:
    result = subprocess.run(
        [
            *_TEMPLATE,
            "--values",
            str(_CHART / "values-kind.yaml"),
            "--set",
            "hub.publicOrigin=http://proxy-public.heartwood-platform.svc.cluster.local",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "publicOrigin" in result.stderr


def test_the_hub_calls_the_proxy_api_over_https_trusting_the_platform_ca(hub: _Hub) -> None:
    import asyncio

    assert hub.config.ConfigurableHTTPProxy.api_url == hub.settings["proxy_api_url"]
    proxy = hub.config.JupyterHub.proxy_class()
    asyncio.run(proxy.api_request("", method="GET"))
    asyncio.run(proxy.api_request("/user/a", method="DELETE"))
    (first, client), (second, again) = hub.proxy_requests
    assert (first, second) == ("GET ", "DELETE /user/a")
    # One client for every request, which trusts the platform CA alone.
    assert client is again
    assert client.options["force_instance"] is True
    defaults = client.options["defaults"]
    assert defaults["ca_certs"] == hub.settings["ca_file"]
    assert defaults["validate_cert"] is True
    assert defaults["prepare_curl_callback"] is not None
