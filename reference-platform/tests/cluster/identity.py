# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Researcher sign-in and token exchange against the installed Keycloak.

Requests are sent with curl from a probe pod in heartwood-sessions, the namespace of the
Heartwood gateways. Passwords, client secrets and tokens travel on standard input and
never on a command line.
"""

from __future__ import annotations

import base64
import html
import json
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from kube import Cluster

SESSIONS = "heartwood-sessions"
PLATFORM = "heartwood-platform"
CONTROL = "http://heartwood-control.heartwood-platform.svc.cluster.local:8080"
KEYCLOAK = "http://keycloak.heartwood-platform.svc.cluster.local:8080"
REALM = f"{KEYCLOAK}/realms/heartwood/protocol/openid-connect"
FIRST = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001"
SECOND = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0002"
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"


@contextmanager
def probe(cluster: Cluster, name: str, image: str) -> Iterator[str]:
    """Run a pod with curl in heartwood-sessions, and the stand-in for the spawner."""
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": SESSIONS},
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
                    "volumeMounts": [{"name": "scratch", "mountPath": "/scratch"}],
                }
            ],
            "volumes": [{"name": "scratch", "emptyDir": {"medium": "Memory"}}],
        },
    }
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "hub", "namespace": SESSIONS},
    }
    existed = cluster.kubectl("get", f"--namespace={SESSIONS}", "serviceaccount/hub")
    try:
        cluster.apply(pod, account)
        ready = cluster.kubectl(
            "wait", f"--namespace={SESSIONS}", "--for=condition=Ready", f"pod/{name}"
        )
        assert ready.returncode == 0, ready.stderr
        yield name
    finally:
        cluster.kubectl("delete", f"--namespace={SESSIONS}", f"pod/{name}", "--wait")
        if existed.returncode != 0:
            cluster.kubectl("delete", f"--namespace={SESSIONS}", "serviceaccount/hub")


def curl(cluster: Cluster, pod: str, *arguments: str, text_input: str = "") -> str:
    """Run curl in the probe pod and return its output."""
    result = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={SESSIONS}",
        pod,
        "--",
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        "60",
        *arguments,
        text_input=text_input,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def request(
    cluster: Cluster,
    pod: str,
    method: str,
    url: str,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """Send a request, with a bearer token and a JSON body when given."""
    headers = ["Accept: application/json, text/event-stream"]
    if token is not None:
        headers.append(f"Authorization: Bearer {token}")
    if body is not None:
        headers.append("Content-Type: application/json")
    # The headers hold the token, so they reach curl through a file that only the
    # probe reads, and the body through standard input.
    path = f"/scratch/{secrets.token_hex(8)}.headers"
    written = cluster.kubectl(
        "exec",
        "--stdin",
        f"--namespace={SESSIONS}",
        pod,
        "--",
        "sh",
        "-c",
        f"umask 077 && cat > {path}",
        text_input="\n".join(headers),
    )
    assert written.returncode == 0, written.stderr
    arguments = ["--request", method, "--header", f"@{path}", "--write-out", "\n%{http_code}"]
    if body is not None:
        arguments += ["--data-binary", "@-"]
    try:
        output = curl(
            cluster, pod, *arguments, url, text_input="" if body is None else json.dumps(body)
        )
    finally:
        cluster.kubectl("exec", f"--namespace={SESSIONS}", pod, "--", "rm", "-f", path)
    text, _, status = output.rpartition("\n")
    try:
        return int(status), json.loads(text)
    except ValueError:
        return int(status), text


def spawner_token(cluster: Cluster) -> str:
    """Return a token of the stand-in for the spawner, for the control service."""
    result = cluster.kubectl(
        "create", "token", "hub", f"--namespace={SESSIONS}", "--audience=heartwood-control"
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def launch_url(subject: str, project: str) -> str:
    return f"{CONTROL}/v1/researchers/{subject}/projects/{project}/launch"


def secret(cluster: Cluster, namespace: str, name: str, key: str) -> str:
    result = cluster.kubectl("get", f"--namespace={namespace}", "secret", name, "--output=json")
    assert result.returncode == 0, result.stderr
    return base64.b64decode(json.loads(result.stdout)["data"][key]).decode()


def claims(token: str) -> dict[str, Any]:
    """Return the claims of a token without verifying it."""
    payload = token.split(".")[1]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert isinstance(decoded, dict)
    return decoded


def token_request(
    cluster: Cluster,
    pod: str,
    form: dict[str, str],
    *,
    endpoint: str = "token",
    basic: tuple[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Send a form to an endpoint of the realm.

    With ``basic``, the client authenticates with HTTP Basic, as the gateway does, and
    the header reaches curl through a file that only the probe reads.
    """
    arguments = ["--data", "@-", "--write-out", "\n%{http_code}"]
    path = None
    if basic is not None:
        path = f"/scratch/{secrets.token_hex(8)}.headers"
        credentials = base64.b64encode(
            f"{quote(basic[0], safe='')}:{quote(basic[1], safe='')}".encode()
        ).decode()
        written = cluster.kubectl(
            "exec",
            "--stdin",
            f"--namespace={SESSIONS}",
            pod,
            "--",
            "sh",
            "-c",
            f"umask 077 && cat > {path}",
            text_input=f"Authorization: Basic {credentials}",
        )
        assert written.returncode == 0, written.stderr
        arguments += ["--header", f"@{path}"]
    try:
        output = curl(cluster, pod, *arguments, f"{REALM}/{endpoint}", text_input=urlencode(form))
    finally:
        if path is not None:
            cluster.kubectl("exec", f"--namespace={SESSIONS}", pod, "--", "rm", "-f", path)
    text, _, status = output.rpartition("\n")
    body = json.loads(text)
    assert isinstance(body, dict)
    return int(status), body


def device_sign_in(cluster: Cluster, pod: str, *, username: str, scope: str) -> dict[str, Any]:
    """Sign a researcher in with the device authorization grant of the command line client.

    The researcher approves the code in a browser, here curl with a cookie jar, and the
    command line client polls the token endpoint as ``heartwood identity token`` does.
    """
    status, grant = token_request(
        cluster, pod, {"client_id": "heartwood-cli", "scope": scope}, endpoint="auth/device"
    )
    assert status == 200, grant.get("error")
    jar = f"/scratch/{secrets.token_hex(8)}.cookies"
    cookies = ("--cookie", jar, "--cookie-jar", jar, "--location")
    try:
        page = curl(cluster, pod, *cookies, grant["verification_uri_complete"])
        form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page)
        assert form is not None, page[:400]
        password = secret(cluster, PLATFORM, "keycloak-researchers", username)
        page = curl(
            cluster,
            pod,
            *cookies,
            "--data",
            "@-",
            html.unescape(form.group(1)),
            text_input=urlencode({"username": username, "password": password}),
        )
        # A public client asks the researcher to grant the requested scopes; the consent
        # form has a relative action and a hidden code.
        consent = re.search(r'<form[^>]*action="([^"]*login-actions/consent[^"]*)"', page)
        if consent is not None:
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page))
            curl(
                cluster,
                pod,
                *cookies,
                "--data",
                "@-",
                urljoin(KEYCLOAK, html.unescape(consent.group(1))),
                text_input=urlencode({**fields, "accept": "Yes"}),
            )
    finally:
        cluster.kubectl("exec", f"--namespace={SESSIONS}", pod, "--", "rm", "-f", jar)
    status, tokens = token_request(
        cluster,
        pod,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": grant["device_code"],
            "client_id": "heartwood-cli",
        },
    )
    assert status == 200, tokens.get("error")
    return tokens


def sign_in(
    cluster: Cluster,
    pod: str,
    *,
    username: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    password: str | None = None,
) -> dict[str, Any]:
    """Sign a researcher in with the authorization code flow, as a browser would.

    Returns the token response of the client, or the page that Keycloak answered the
    sign-in form with when it issued no code.
    """
    jar = f"/scratch/{secrets.token_hex(8)}.cookies"
    state = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid",
            "state": state,
        }
    )
    page = curl(cluster, pod, "--cookie-jar", jar, f"{REALM}/auth?{query}")
    form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page)
    assert form is not None, page[:400]
    if password is None:
        password = secret(cluster, PLATFORM, "keycloak-researchers", username)
    answer = curl(
        cluster,
        pod,
        "--cookie",
        jar,
        "--cookie-jar",
        jar,
        "--data",
        "@-",
        "--write-out",
        "\n%{redirect_url}",
        html.unescape(form.group(1)),
        text_input=urlencode({"username": username, "password": password}),
    )
    cluster.kubectl("exec", f"--namespace={SESSIONS}", pod, "--", "rm", "-f", jar)
    body, _, redirect = answer.rpartition("\n")
    if not redirect.startswith(redirect_uri):
        return {"error": "no-code", "page": body}
    returned = parse_qs(urlsplit(redirect).query)
    assert returned["state"] == [state]
    status, tokens = token_request(
        cluster,
        pod,
        {
            "grant_type": "authorization_code",
            "code": returned["code"][0],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert status == 200, tokens.get("error")
    return tokens


def exchange(
    cluster: Cluster,
    pod: str,
    *,
    client_id: str,
    client_secret: str,
    subject_token: str,
    service: str | None,
    audience: str | None = None,
    basic: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Exchange a researcher token for a token of one platform service.

    The client authenticates in the form, or with HTTP Basic as the gateway does.
    """
    form = {
        "grant_type": EXCHANGE,
        **({} if basic else {"client_id": client_id, "client_secret": client_secret}),
        "subject_token": subject_token,
        "subject_token_type": ACCESS_TOKEN,
        "requested_token_type": ACCESS_TOKEN,
    }
    if service is not None:
        form["scope"] = f"svc-{service}"
    if audience is not None:
        form["audience"] = audience
    return token_request(cluster, pod, form, basic=(client_id, client_secret) if basic else None)
