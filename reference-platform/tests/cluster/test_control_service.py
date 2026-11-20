# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The control service on the cluster: callers, the registry and the project client.

A ServiceAccount hub in heartwood-sessions stands in for the spawner. Requests are sent
from a pod in heartwood-sessions, the namespace that the network policy admits.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlencode

import pytest
from kube import PROBE_CA, Cluster

_SESSIONS = "heartwood-sessions"
_CONTROL = "https://heartwood-control.heartwood-platform.svc.cluster.local:8443"
_KEYCLOAK = "https://keycloak.heartwood-platform.svc.cluster.local:8443"
_FIRST = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001"
_SECOND = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0002"
_POD = "hw-control-probe"


@pytest.fixture(scope="module", autouse=True)
def caller(cluster: Cluster, probe_image: str) -> Iterator[None]:
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": _POD, "namespace": _SESSIONS},
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
                    "image": probe_image,
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
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "hub", "namespace": _SESSIONS},
    }
    existed = cluster.kubectl("get", f"--namespace={_SESSIONS}", "serviceaccount/hub")
    cluster.apply(pod, account)
    ready = cluster.kubectl(
        "wait", f"--namespace={_SESSIONS}", "--for=condition=Ready", f"pod/{_POD}"
    )
    assert ready.returncode == 0, ready.stderr
    yield
    for subject, project in ((_FIRST, "demo"), (_FIRST, "other"), (_SECOND, "demo")):
        _call(cluster, "DELETE", _launch(subject, project), _token(cluster))
    cluster.kubectl("delete", f"--namespace={_SESSIONS}", f"pod/{_POD}", "--wait")
    if existed.returncode != 0:
        cluster.kubectl("delete", f"--namespace={_SESSIONS}", "serviceaccount/hub")


def _token(cluster: Cluster, account: str = "hub", audience: str = "heartwood-control") -> str:
    result = cluster.kubectl(
        "create", "token", account, f"--namespace={_SESSIONS}", f"--audience={audience}"
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _call(cluster: Cluster, method: str, url: str, token: str | None = None) -> tuple[int, Any]:
    """Send a request from the probe pod. The token travels on standard input."""
    header = f"Authorization: Bearer {token}" if token is not None else "Accept: */*"
    result = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={_SESSIONS}",
        _POD,
        "--",
        "curl",
        "--silent",
        "--max-time",
        "30",
        "--cacert",
        "/etc/platform-ca/ca.crt",
        "--request",
        method,
        "--header",
        "@-",
        "--write-out",
        "\n%{http_code}",
        url,
        text_input=header,
    )
    assert result.returncode == 0, result.stderr
    body, _, status = result.stdout.rpartition("\n")
    try:
        return int(status), json.loads(body)
    except json.JSONDecodeError:
        return int(status), body


def _launch(subject: str, project: str) -> str:
    return f"{_CONTROL}/v1/researchers/{subject}/projects/{project}/launch"


def _redirect_uri_is_registered(cluster: Cluster, project: str, uri: str) -> bool:
    """Ask Keycloak's authorization endpoint, which refuses an unregistered URI."""
    url = (
        f"{_KEYCLOAK}/realms/heartwood/protocol/openid-connect/auth?response_type=code"
        f"&scope=openid&client_id=hw-proj-{project}&redirect_uri={quote(uri, safe='')}"
    )
    status, _ = _call(cluster, "GET", url)
    assert status in {200, 400}, status
    return status == 200


def _project_client(cluster: Cluster, project: str) -> dict[str, Any]:
    """Read the client of a project with the control service's own Keycloak rights."""
    secret = cluster.kubectl(
        "get",
        "--namespace=heartwood-platform",
        "secret",
        "heartwood-control-keycloak",
        "--output=jsonpath={.data.client-secret}",
    )
    assert secret.returncode == 0, secret.stderr
    form = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": "heartwood-control",
            "client_secret": base64.b64decode(secret.stdout).decode(),
            "scope": "kc-admin-api",
        }
    )
    token = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={_SESSIONS}",
        _POD,
        "--",
        "curl",
        "--silent",
        "--max-time",
        "30",
        "--cacert",
        "/etc/platform-ca/ca.crt",
        "--data",
        "@-",
        f"{_KEYCLOAK}/realms/heartwood/protocol/openid-connect/token",
        text_input=form,
    )
    assert token.returncode == 0, token.stderr
    url = f"{_KEYCLOAK}/admin/realms/heartwood/clients?clientId=hw-proj-{project}"
    status, clients = _call(cluster, "GET", url, json.loads(token.stdout)["access_token"])
    assert status == 200, clients
    (client,) = clients
    assert isinstance(client, dict)
    return client


def test_project_client_is_confidential_with_token_exchange_and_service_scopes(
    cluster: Cluster,
) -> None:
    status, launch = _call(cluster, "PUT", _launch(_FIRST, "other"), _token(cluster))
    assert status == 200, launch

    client = _project_client(cluster, "other")

    assert client["publicClient"] is False
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["attributes"]["standard.token.exchange.enabled"] == "true"
    assert {"svc-control", "svc-data", "svc-jobs"} <= set(client["optionalClientScopes"])
    assert client["redirectUris"] == [launch["redirect_uri"]]


def test_spawner_launches_a_member_and_the_project_client_is_prepared(cluster: Cluster) -> None:
    token = _token(cluster)

    status, launch = _call(cluster, "PUT", _launch(_FIRST, "demo"), token)

    assert status == 200, launch
    assert re.fullmatch(r"[0-9a-f]{16}", launch["session"])
    assert launch["client_id"] == "hw-proj-demo"
    assert launch["launch_context"]["project_id"] == "demo"
    assert launch["launch_context"]["billing_attribution_ref"] == "synthetic-grant-demo"
    assert launch["launch_context"]["project_context_id"] == launch["session"]
    assert _redirect_uri_is_registered(cluster, "demo", launch["redirect_uri"])
    assert not _redirect_uri_is_registered(cluster, "demo", launch["redirect_uri"] + "/other")

    assert _call(cluster, "PUT", _launch(_FIRST, "demo"), token)[1]["session"] == launch["session"]
    status, second = _call(cluster, "PUT", _launch(_SECOND, "demo"), token)
    assert status == 200
    assert second["session"] != launch["session"]
    assert _redirect_uri_is_registered(cluster, "demo", launch["redirect_uri"])
    assert _redirect_uri_is_registered(cluster, "demo", second["redirect_uri"])

    assert _call(cluster, "DELETE", _launch(_FIRST, "demo"), token)[0] == 204
    assert not _redirect_uri_is_registered(cluster, "demo", launch["redirect_uri"])
    assert _redirect_uri_is_registered(cluster, "demo", second["redirect_uri"])


def test_spawner_lists_the_projects_of_a_researcher(cluster: Cluster) -> None:
    token = _token(cluster)

    def projects(subject: str) -> Any:  # noqa: ANN401
        return _call(cluster, "GET", f"{_CONTROL}/v1/researchers/{subject}/projects", token)

    assert projects(_FIRST) == (200, {"projects": ["demo", "other"]})
    assert projects(_SECOND) == (200, {"projects": ["demo"]})


def test_launch_outside_the_registry_is_refused(cluster: Cluster) -> None:
    token = _token(cluster)

    assert _call(cluster, "PUT", _launch(_SECOND, "other"), token)[0] == 403
    assert _call(cluster, "PUT", _launch(_SECOND, "missing"), token)[0] == 403
    assert not _redirect_uri_is_registered(
        cluster,
        "other",
        f"http://proxy-public.{_SESSIONS}.svc.cluster.local/user/{_SECOND}/other/oauth_callback",
    )


def test_other_callers_are_refused(cluster: Cluster) -> None:
    for token in (
        None,
        "not-a-token",
        _token(cluster, audience="heartwood-data"),
        _token(cluster, audience="https://kubernetes.default.svc.cluster.local"),
        _token(cluster, account="default"),
    ):
        assert _call(cluster, "PUT", _launch(_FIRST, "demo"), token)[0] == 401
        assert _call(cluster, "GET", f"{_CONTROL}/v1/launch-context", token)[0] == 401


def test_control_service_serves_only_https_with_a_certificate_of_the_platform_ca(
    cluster: Cluster,
) -> None:
    # _call trusts only the platform CA, so an answer shows the certificate of the CA.
    assert _call(cluster, "GET", f"{_CONTROL}/v1/launch-context")[0] == 401
    # Without the platform CA the certificate is not trusted (curl exit 60), and plain HTTP
    # receives no HTTP answer (exit 52, an empty reply, or 56, a reset connection).
    for url, exits in (
        (f"{_CONTROL}/healthz", {60}),
        (_CONTROL.replace("https", "http") + "/healthz", {52, 56}),
    ):
        result = cluster.kubectl(
            "exec",
            f"--namespace={_SESSIONS}",
            _POD,
            "--",
            "curl",
            "--silent",
            "--max-time",
            "10",
            "--output",
            "/dev/null",
            url,
        )
        assert result.returncode in exits, (url, result.returncode, result.stderr)
    # The service itself listens on no plain HTTP port.
    result = cluster.kubectl(
        "exec",
        "--namespace=heartwood-platform",
        "statefulset/heartwood-control",
        "--",
        "/opt/heartwood-platform/.venv/bin/python",
        "-c",
        "import socket; socket.create_connection(('127.0.0.1', 8080), 5)",
    )
    assert result.returncode != 0
    assert "ConnectionRefusedError" in result.stderr
