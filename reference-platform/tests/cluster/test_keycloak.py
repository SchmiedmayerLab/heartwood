# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The state of the installed Keycloak: no administrator, and the imported realm."""

from __future__ import annotations

import base64
import json
import warnings
from typing import Any
from urllib.parse import urlencode

from kube import Cluster

_KEYCLOAK = "http://keycloak.heartwood-platform.svc.cluster.local:8080"
# The exit status of the request script when the form reached it incomplete.
_INCOMPLETE = 75


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


def _token(cluster: Cluster, realm: str, form: dict[str, str]) -> dict[str, Any]:
    """Request a token from inside the Keycloak pod, with the form on standard input.

    Data sent to ``kubectl exec`` on standard input can arrive incomplete, so the script
    refuses a form of another length, and the request is repeated with a warning.
    """
    body = urlencode(form)
    script = (
        f'exec 3<>/dev/tcp/127.0.0.1/8080; body=$(cat); [ "${{#body}}" -eq {len(body)} ] '
        f"|| exit {_INCOMPLETE}; "
        f'printf "POST /realms/{realm}/protocol/openid-connect/token HTTP/1.0\\r\\n'
        "Host: keycloak:8080\\r\\n"
        "Content-Type: application/x-www-form-urlencoded\\r\\n"
        'Content-Length: %s\\r\\n\\r\\n%s" "${#body}" "${body}" >&3; cat <&3'
    )
    for attempt in range(1, 6):
        result = cluster.kubectl(
            "exec",
            "--stdin",
            "--namespace=heartwood-platform",
            "deployment/keycloak",
            "--",
            "/bin/bash",
            "-c",
            script,
            text_input=body,
        )
        if result.returncode != _INCOMPLETE:
            break
        warnings.warn(
            f"form {attempt} reached Keycloak incomplete: {len(body)} bytes sent", stacklevel=2
        )
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout[result.stdout.index("{") :])
    assert isinstance(answer, dict)
    return answer


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
