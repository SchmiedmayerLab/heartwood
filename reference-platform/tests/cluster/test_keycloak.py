# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The state of the installed Keycloak: no administrator, and the imported realm."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
from kube import PROBE_CA, Cluster

_KEYCLOAK = "https://keycloak.heartwood-platform.svc.cluster.local:8443"
_SESSIONS = "heartwood-sessions"
_POD = "hw-keycloak-probe"


@pytest.fixture(scope="module", autouse=True)
def probe(cluster: Cluster, probe_image: str) -> Iterator[None]:
    """A pod in the sessions namespace that trusts only the platform CA."""
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
    try:
        cluster.apply(pod)
        ready = cluster.kubectl(
            "wait", f"--namespace={_SESSIONS}", "--for=condition=Ready", f"pod/{_POD}"
        )
        assert ready.returncode == 0, ready.stderr
        yield
    finally:
        cluster.kubectl("delete", f"--namespace={_SESSIONS}", f"pod/{_POD}", "--wait")


def _secret(cluster: Cluster, name: str, key: str) -> str:
    result = cluster.kubectl(
        "get",
        "--namespace=heartwood-platform",
        "secret",
        name,
        f"--output=jsonpath={{.data.{key}}}",
    )
    assert result.returncode == 0, result.stderr
    return base64.b64decode(result.stdout).decode()


def _curl(cluster: Cluster, *arguments: str, text_input: str = "") -> str:
    result = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={_SESSIONS}",
        _POD,
        "--",
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        "30",
        "--cacert",
        "/etc/platform-ca/ca.crt",
        *arguments,
        text_input=text_input,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _token(cluster: Cluster, realm: str, form: dict[str, str]) -> dict[str, Any]:
    """Request a token over TLS, with the form on standard input."""
    output = _curl(
        cluster,
        "--data",
        "@-",
        f"{_KEYCLOAK}/realms/{realm}/protocol/openid-connect/token",
        text_input=urlencode(form),
    )
    body = json.loads(output)
    assert isinstance(body, dict)
    return body


def test_keycloak_serves_https_with_a_certificate_of_the_platform_ca(cluster: Cluster) -> None:
    # curl trusts only the platform CA, so the request succeeds only with its certificate.
    document = json.loads(
        _curl(cluster, f"{_KEYCLOAK}/realms/heartwood/.well-known/openid-configuration")
    )
    assert document["issuer"] == f"{_KEYCLOAK}/realms/heartwood"
    # Plain HTTP is not served.
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
        "--write-out",
        "%{http_code}",
        "http://keycloak.heartwood-platform.svc.cluster.local:8443/realms/heartwood",
    )
    assert result.stdout.strip() in {"000", "400"}, result.stdout
    # Keycloak itself listens on no plain HTTP port.
    result = cluster.kubectl(
        "exec",
        "--namespace=heartwood-platform",
        "deployment/keycloak",
        "--",
        "/bin/bash",
        "-c",
        "! (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null",
    )
    assert result.returncode == 0, "Keycloak accepts connections on port 8080"


def test_bootstrap_administrator_is_disabled(cluster: Cluster) -> None:
    response = _token(
        cluster,
        "master",
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _secret(cluster, "keycloak-bootstrap-admin", "username"),
            "password": _secret(cluster, "keycloak-bootstrap-admin", "password"),
        },
    )

    assert response == {"error": "invalid_grant", "error_description": "Account disabled"}


def test_control_client_receives_an_administration_token_for_its_realm(cluster: Cluster) -> None:
    response = _token(
        cluster,
        "heartwood",
        {
            "grant_type": "client_credentials",
            "client_id": "heartwood-control",
            "client_secret": _secret(cluster, "heartwood-control-keycloak", "client-secret"),
            "scope": "kc-admin-api",
        },
    )

    payload = response["access_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["azp"] == "heartwood-control"
    assert claims["iss"] == f"{_KEYCLOAK}/realms/heartwood"
    assert f"{_KEYCLOAK}/admin/realms/heartwood" in claims["aud"]
    assert "kc-admin-api" in claims["scope"].split()
