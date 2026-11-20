# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The local model behind LiteLLM on the cluster.

llama-server serves HTTPS with a certificate of the platform CA and requires its API key
for completions. LiteLLM reaches it with that key; other pods cannot connect. A runtime
without the platform's certificate receives no request, and a stopped runtime fails the
request.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from identity import PLATFORM, SESSIONS, probe, secret
from kube import Cluster
from test_model_route import GATEWAY_ROLE, WORKER_ROUTES, Proxy

LOCAL = "local-model"
_SERVER = "llama-server.heartwood-platform.svc.cluster.local"


@pytest.fixture(scope="module")
def proxy(cluster: Cluster, probe_image: str) -> Iterator[Proxy]:
    with probe(cluster, "hw-local-probe", probe_image, GATEWAY_ROLE) as pod:
        yield Proxy(cluster, pod)


@contextmanager
def _forwarded(cluster: Cluster) -> Iterator[int]:
    """Forward a local port to llama-server, past the network policy, as an operator can."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    forward = subprocess.Popen(
        [
            "kubectl",
            "--context",
            cluster.context,
            f"--namespace={PLATFORM}",
            "port-forward",
            "deployment/llama-server",
            f"{port}:8443",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with socket.socket() as attempt:
                if attempt.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.5)
        yield port
    finally:
        forward.terminate()
        forward.wait(timeout=10)


def _status(port: int, ca: Path, method: str, path: str, key: str | None = None) -> int:
    arguments = [
        "curl",
        "--silent",
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_code}",
        "--cacert",
        str(ca),
        "--resolve",
        f"{_SERVER}:{port}:127.0.0.1",
        "--request",
        method,
        f"https://{_SERVER}:{port}{path}",
    ]
    if method == "POST":
        arguments += [
            "--header",
            "Content-Type: application/json",
            "--data",
            json.dumps({"messages": [{"role": "user", "content": "Once"}]}),
        ]
    if key is not None:
        # The key reaches curl through a file, never through its arguments.
        arguments += ["--header", "@-"]
    result = subprocess.run(
        arguments,
        input=None if key is None else f"Authorization: Bearer {key}",
        capture_output=True,
        text=True,
        timeout=60,
    )
    return int(result.stdout or 0)


def test_the_runtime_requires_its_key_for_completions_and_lists_models_without_it(
    cluster: Cluster, tmp_path: Path
) -> None:
    ca = tmp_path / "platform-ca.crt"
    result = cluster.kubectl(
        "get", f"--namespace={PLATFORM}", "configmap", "platform-ca", "--output=json"
    )
    ca.write_text(json.loads(result.stdout)["data"]["ca.crt"])
    key = secret(cluster, PLATFORM, "llama-server", "api-key")
    with _forwarded(cluster) as port:
        assert _status(port, ca, "GET", "/health") == 200
        assert _status(port, ca, "GET", "/v1/models") == 200
        assert _status(port, ca, "POST", "/v1/chat/completions") == 401
        assert _status(port, ca, "POST", "/v1/chat/completions", "not-the-key") == 401
        assert _status(port, ca, "POST", "/v1/chat/completions", key) == 200
        # Without the web interface, its page is not served.
        assert _status(port, ca, "GET", "/") != 200


def test_other_pods_cannot_reach_the_runtime(cluster: Cluster, probe_image: str) -> None:
    for labels in (None, GATEWAY_ROLE):
        with probe(cluster, "hw-local-outsider", probe_image, labels) as pod:
            result = cluster.kubectl(
                "exec",
                f"--namespace={SESSIONS}",
                pod,
                "--",
                "curl",
                "--silent",
                "--insecure",
                "--connect-timeout",
                "5",
                f"https://{_SERVER}:8443/health",
            )
            assert result.returncode == 28


def test_a_key_reaches_the_local_model_through_litellm(proxy: Proxy) -> None:
    key = proxy.generate([LOCAL], WORKER_ROUTES, 1.0)
    try:
        status, answer = proxy.chat(key, LOCAL)
        assert status == 200, answer
        assert answer["choices"][0]["message"]["role"] == "assistant"
        assert answer["usage"]["completion_tokens"] > 0
    finally:
        proxy.call(proxy.master, "POST", "/key/delete", {"keys": [key]})


def _scale(cluster: Cluster, replicas: int) -> None:
    result = cluster.kubectl(
        "scale", f"--namespace={PLATFORM}", "deployment/llama-server", f"--replicas={replicas}"
    )
    assert result.returncode == 0, result.stderr
    if replicas:
        result = cluster.kubectl(
            "rollout",
            "status",
            f"--namespace={PLATFORM}",
            "deployment/llama-server",
            "--timeout=300s",
        )
    else:
        result = cluster.kubectl(
            "wait",
            f"--namespace={PLATFORM}",
            "--for=delete",
            "pod",
            "--selector=app.kubernetes.io/component=llama-server",
            "--timeout=120s",
        )
    assert result.returncode == 0, result.stderr


def _squatter(cluster: Cluster, probe_image: str, directory: Path) -> dict[str, object]:
    """A server with the runtime's labels and a certificate from another authority."""
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={_SERVER}",
            "-addext",
            f"subjectAltName=DNS:{_SERVER}",
            "-keyout",
            str(directory / "tls.key"),
            "-out",
            str(directory / "tls.crt"),
        ],
        check=True,
        capture_output=True,
    )
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "hw-squatter-tls", "namespace": PLATFORM},
            "type": "kubernetes.io/tls",
            "data": {
                name: base64.b64encode((directory / name).read_bytes()).decode()
                for name in ("tls.crt", "tls.key")
            },
        }
    )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "hw-squatter",
            "namespace": PLATFORM,
            "labels": {
                "app.kubernetes.io/name": "heartwood-platform",
                "app.kubernetes.io/instance": "heartwood-platform",
                "app.kubernetes.io/component": "llama-server",
            },
        },
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "squatter",
                    "image": probe_image,
                    "args": [
                        "netexec",
                        "--http-port=8443",
                        "--tls-cert-file=/tls/tls.crt",
                        "--tls-private-key-file=/tls/tls.key",
                    ],
                    "ports": [{"name": "https", "containerPort": 8443}],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "tls", "mountPath": "/tls", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "tls", "secret": {"secretName": "hw-squatter-tls"}}],
        },
    }


def test_a_stopped_or_squatted_runtime_fails_the_request_and_receives_no_prompt(
    proxy: Proxy, probe_image: str
) -> None:
    cluster = proxy.cluster
    key = proxy.generate([LOCAL], WORKER_ROUTES, 1.0)
    try:
        _scale(cluster, 0)
        status, answer = proxy.chat(key, LOCAL)
        assert status >= 500, answer
        with TemporaryDirectory(prefix="squatter-") as name:
            cluster.apply(_squatter(cluster, probe_image, Path(name)))
        ready = cluster.kubectl(
            "wait",
            f"--namespace={PLATFORM}",
            "--for=condition=Ready",
            "pod/hw-squatter",
            "--timeout=120s",
        )
        assert ready.returncode == 0, ready.stderr
        status, answer = proxy.chat(key, LOCAL)
        assert status >= 500, answer
        logs = cluster.kubectl("logs", f"--namespace={PLATFORM}", "pod/hw-squatter").stdout
        assert "chat/completions" not in logs
        assert "Say hello" not in logs
    finally:
        cluster.kubectl("delete", f"--namespace={PLATFORM}", "pod/hw-squatter", "--wait")
        cluster.kubectl("delete", f"--namespace={PLATFORM}", "secret/hw-squatter-tls")
        _scale(cluster, 1)
        proxy.call(proxy.master, "POST", "/key/delete", {"keys": [key]})
    status, _ = proxy.chat(key, LOCAL)
    assert status == 401
