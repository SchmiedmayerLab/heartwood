# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Access from the test host to a project's Funnel server and to the object store.

Both are reached through ``kubectl port-forward``. The Funnel server is verified against
the platform CA under the name of its Service, and object store requests carry the host
of the in-cluster address that their signatures name. Secrets are read from the cluster
and never printed.
"""

from __future__ import annotations

import base64
import re
import ssl
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from heartwood_platform.common.signing import Credentials, presign
from heartwood_platform.jobs.tes import HttpTesExecutor, TesTaskView
from kube import Cluster

PLATFORM = "heartwood-platform"
STORE = "https://versitygw.heartwood-platform.svc.cluster.local:7070"
# A task image that the kind values list, with a shell.
TASK_IMAGE = (
    "registry.k8s.io/e2e-test-images/agnhost:2.53"
    "@sha256:99c6b4bb4a1e1df3f0b3752168c89358794d02258ebebc26bf21c29399011a85"
)
TERMINAL = {"COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED", "PREEMPTED"}
_ORDER = {
    "UNKNOWN": 0,
    "QUEUED": 1,
    "INITIALIZING": 2,
    "RUNNING": 3,
    "PAUSED": 3,
    "CANCELING": 4,
    **dict.fromkeys(TERMINAL, 5),
}


def secret(cluster: Cluster, namespace: str, name: str, key: str) -> str:
    """Return one value of a Secret."""
    result = cluster.kubectl(
        "get", f"--namespace={namespace}", f"secret/{name}", f"--output=jsonpath={{.data.{key}}}"
    )
    assert result.returncode == 0, result.stderr
    return base64.b64decode(result.stdout).decode()


@contextmanager
def forward(cluster: Cluster, namespace: str, target: str, port: int) -> Iterator[int]:
    """Forward a local port to a Service or pod and yield the local port."""
    process = subprocess.Popen(
        [
            "kubectl",
            "--context",
            cluster.context,
            f"--namespace={namespace}",
            "port-forward",
            target,
            f":{port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        match = re.search(r"127\.0\.0\.1:(\d+)", line)
        assert match is not None, line
        yield int(match.group(1))
    finally:
        process.terminate()
        process.wait(timeout=30)


class _Forwarded(httpx.HTTPTransport):
    """Send requests for one host to a forwarded local port, keeping their host name."""

    def __init__(self, port: int, **options: Any) -> None:  # noqa: ANN401
        super().__init__(**options)
        self._port = port

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        request.headers["Host"] = request.url.netloc.decode()
        request.url = request.url.copy_with(host="127.0.0.1", port=self._port)
        # The certificate is verified for the original host name.
        request.extensions = {**request.extensions, "sni_hostname": host}
        return super().handle_request(request)


def platform_ca(cluster: Cluster, directory: Path) -> Path:
    """Write the platform CA certificate to a file and return its path."""
    result = cluster.kubectl(
        "get",
        f"--namespace={PLATFORM}",
        "configmap/platform-ca",
        "--output=jsonpath={.data.ca\\.crt}",
    )
    assert result.returncode == 0, result.stderr
    path = directory / "platform-ca.crt"
    path.write_text(result.stdout, encoding="utf-8")
    return path


@dataclass
class Executor:
    """A project's Funnel server reached from the test host."""

    url: str
    ca_file: Path
    port: int

    def client(self, user: str, password: str) -> HttpTesExecutor:
        transport = _Forwarded(self.port, verify=ssl.create_default_context(cafile=self.ca_file))
        return HttpTesExecutor(self.url, self.ca_file, user, password, transport=transport)

    def http(self, user: str, password: str) -> httpx.Client:
        """A plain client of the same API, for requests outside the TES subset."""
        transport = _Forwarded(self.port, verify=ssl.create_default_context(cafile=self.ca_file))
        return httpx.Client(
            base_url=self.url, auth=(user, password), transport=transport, timeout=30
        )


@contextmanager
def executor(cluster: Cluster, project: str) -> Iterator[Executor]:
    """Reach a project's Funnel server under its Service name."""
    namespace = f"hw-jobs-{project}"
    with (
        tempfile.TemporaryDirectory() as directory,
        forward(cluster, namespace, "service/funnel-server", 8000) as port,
    ):
        yield Executor(
            f"https://funnel-server.{namespace}.svc:8000",
            platform_ca(cluster, Path(directory)),
            port,
        )


def job_service_password(cluster: Cluster, project: str) -> str:
    """The job service's Basic password at a project's executor."""
    return secret(cluster, PLATFORM, "heartwood-jobs-executors", project)


class Store:
    """The object store, with the root account, through a forwarded port."""

    def __init__(self, cluster: Cluster, port: int, ca_file: Path) -> None:
        self._root = Credentials(
            secret(cluster, PLATFORM, "versitygw-root", "access-key"),
            secret(cluster, PLATFORM, "versitygw-root", "secret-key"),
        )
        # The store serves HTTPS with a certificate of the platform CA for its Service name.
        transport = _Forwarded(port, verify=ssl.create_default_context(cafile=ca_file))
        self._client = httpx.Client(transport=transport, timeout=30)

    def url(self, method: str, bucket: str, key: str, expires: int = 900) -> str:
        """A presigned URL of the in-cluster address."""
        return presign(
            method=method,
            endpoint=STORE,
            path=f"/{bucket}/{key}",
            credentials=self._root,
            region="us-east-1",
            expires=expires,
            now=datetime.now(UTC),
        )

    def put(self, bucket: str, key: str, content: bytes) -> None:
        response = self._client.put(self.url("PUT", bucket, key), content=content)
        assert response.status_code == 200, response.text

    def get(self, bucket: str, key: str) -> bytes | None:
        response = self._client.get(self.url("GET", bucket, key))
        if response.status_code == 404:
            return None
        assert response.status_code == 200, response.text
        return response.content


@contextmanager
def store(cluster: Cluster) -> Iterator[Store]:
    with (
        tempfile.TemporaryDirectory() as directory,
        forward(cluster, PLATFORM, "service/versitygw", 7070) as port,
    ):
        yield Store(cluster, port, platform_ca(cluster, Path(directory)))


def wait_for(
    observe: Callable[[], TesTaskView],
    done: Callable[[TesTaskView], bool],
    timeout: float = 300,
) -> tuple[TesTaskView, list[str]]:
    """Poll a task until ``done``, checking that its states only move forward."""
    states: list[str] = []
    deadline = time.monotonic() + timeout
    while True:
        view = observe()
        if not states or states[-1] != view.state:
            if states:
                assert _ORDER[view.state] >= _ORDER[states[-1]], [*states, view.state]
            states.append(view.state)
        if done(view):
            return view, states
        assert time.monotonic() < deadline, states
        time.sleep(2)
