# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The model route through LiteLLM on the cluster.

LiteLLM serves HTTPS with a certificate of the platform CA and accepts only the control
service, the supervisors of session sandboxes and the gateway pods. A key made like a
session's worker key reaches only the inference routes and its models, and LiteLLM
refuses it beyond its budget. A launch gives the session's gateway a discovery key that
may only list models, and a deletion revokes it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from identity import (
    FIRST,
    PLATFORM,
    SESSIONS,
    curl,
    launch_url,
    probe,
    request,
    secret,
    spawner_token,
)
from kube import Cluster

LITELLM = "https://litellm.heartwood-platform.svc.cluster.local:4000"
SYNTHETIC = "synthetic-coder"
# The probe stands where a Heartwood gateway pod would, which LiteLLM admits.
GATEWAY_ROLE = {"heartwood.platform/role": "hosted-gateway"}
WORKER_ROUTES = ["/v1/chat/completions", "/v1/models"]


class Proxy:
    """Calls to LiteLLM from a probe pod that LiteLLM admits."""

    def __init__(self, cluster: Cluster, pod: str) -> None:
        self.cluster = cluster
        self.pod = pod
        self.master = secret(cluster, PLATFORM, "litellm", "master-key")

    def call(
        self, key: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        return request(self.cluster, self.pod, method, f"{LITELLM}{path}", key, body)

    def generate(self, models: list[str], routes: list[str], budget: float) -> str:
        status, answer = self.call(
            self.master,
            "POST",
            "/key/generate",
            {"models": models, "allowed_routes": routes, "max_budget": budget},
        )
        assert status == 200, answer
        key = answer["key"]
        assert isinstance(key, str)
        return key

    def chat(self, key: str, model: str = SYNTHETIC) -> tuple[int, Any]:
        body = {"model": model, "messages": [{"role": "user", "content": "Say hello."}]}
        return self.call(key, "POST", "/v1/chat/completions", body)


@pytest.fixture(scope="module")
def proxy(cluster: Cluster, probe_image: str) -> Iterator[Proxy]:
    with probe(cluster, "hw-route-probe", probe_image, GATEWAY_ROLE) as pod:
        yield Proxy(cluster, pod)


def _hash(key: str) -> str:
    """Return the SHA-256 hash of a key, under which LiteLLM stores it."""
    return hashlib.sha256(key.encode()).hexdigest()


def test_litellm_serves_only_https_with_a_certificate_of_the_platform_ca(proxy: Proxy) -> None:
    assert curl(proxy.cluster, proxy.pod, f"{LITELLM}/health/liveliness").strip()
    untrusted = proxy.cluster.kubectl(
        "exec",
        f"--namespace={SESSIONS}",
        proxy.pod,
        "--",
        "curl",
        "--silent",
        "--max-time",
        "20",
        f"{LITELLM}/health/liveliness",
    )
    # 60: the certificate does not verify against the default certificate authorities.
    assert untrusted.returncode == 60
    plain = proxy.cluster.kubectl(
        "exec",
        f"--namespace={SESSIONS}",
        proxy.pod,
        "--",
        "curl",
        "--silent",
        "--max-time",
        "20",
        "http://litellm.heartwood-platform.svc.cluster.local:4000/health/liveliness",
    )
    assert plain.returncode in {52, 56}


def test_pods_without_an_allowance_cannot_reach_litellm_or_its_models(
    cluster: Cluster, probe_image: str
) -> None:
    targets = (
        "litellm.heartwood-platform.svc.cluster.local:4000",
        "synthetic-model.heartwood-platform.svc.cluster.local:8443",
    )
    with probe(cluster, "hw-route-outsider", probe_image) as pod:
        for target in targets:
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
                f"https://{target}/",
            )
            # 28: the connection timed out, because the network policy drops it.
            assert result.returncode == 28, (target, result.returncode)
    # The gateway probe reaches LiteLLM, but not the models behind it.
    with probe(cluster, "hw-route-gateway", probe_image, GATEWAY_ROLE) as pod:
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
            f"https://{targets[1]}/",
        )
        assert result.returncode == 28


def test_a_worker_key_reaches_only_inference_routes_and_its_models(proxy: Proxy) -> None:
    key = proxy.generate([SYNTHETIC], WORKER_ROUTES, 1.0)
    try:
        status, listed = proxy.call(key, "GET", "/v1/models")
        assert status == 200
        assert [item["id"] for item in listed["data"]] == [SYNTHETIC]
        for path in ("/key/info", "/model/info", "/health"):
            status, answer = proxy.call(key, "GET", path)
            assert status == 403, (path, answer)
        status, answer = proxy.call(key, "POST", "/key/generate", {"models": [SYNTHETIC]})
        assert status in {401, 403}, answer
        status, answer = proxy.chat(key)
        assert status == 200, answer
        assert answer["usage"]["prompt_tokens"] == 1000
    finally:
        proxy.call(proxy.master, "POST", "/key/delete", {"keys": [key]})


def test_a_model_outside_the_key_list_is_refused(proxy: Proxy) -> None:
    key = proxy.generate([SYNTHETIC], WORKER_ROUTES, 1.0)
    try:
        status, answer = proxy.chat(key, "local-model")
        assert status in {401, 403}, answer
        assert answer["error"]["type"] == "key_model_access_denied"
    finally:
        proxy.call(proxy.master, "POST", "/key/delete", {"keys": [key]})


def test_a_key_beyond_its_budget_is_refused_and_its_spending_is_recorded(proxy: Proxy) -> None:
    # One request costs 0.00125: 1000 input tokens at 0.50 and 500 output tokens at 1.50
    # per million.
    key = proxy.generate([SYNTHETIC], WORKER_ROUTES, 0.002)
    token = _hash(key)
    try:
        assert proxy.chat(key)[0] == 200
        # The second request is admitted below the budget and ends above it.
        assert proxy.chat(key)[0] == 200
        status, answer = proxy.chat(key)
        assert status == 422, answer
        assert answer["error"]["type"] == "budget_exceeded"
        # The control service refers to the key by its hash only.
        deadline = time.monotonic() + 60
        spend = 0.0
        while time.monotonic() < deadline:
            status, info = proxy.call(proxy.master, "GET", f"/key/info?key={token}")
            assert status == 200, info
            spend = info["info"]["spend"]
            if spend >= 0.0025:
                break
            time.sleep(5)
        assert spend == pytest.approx(0.0025)
        # A budget of the recorded spending blocks the key, as at settlement.
        status, _ = proxy.call(
            proxy.master, "POST", "/key/update", {"key": token, "max_budget": spend}
        )
        assert status == 200
        assert proxy.chat(key)[0] == 422
    finally:
        status, _ = proxy.call(proxy.master, "POST", "/key/delete", {"keys": [token]})
    assert status == 200
    assert proxy.chat(key)[0] == 401


def _launch(proxy: Proxy) -> dict[str, Any]:
    """Launch the first researcher's session of demo, which waits for its gateway."""
    status, launch = request(
        proxy.cluster,
        proxy.pod,
        "PUT",
        launch_url(FIRST, "demo"),
        spawner_token(proxy.cluster),
        max_time=300,
    )
    assert status == 200, launch
    assert isinstance(launch, dict)
    return launch


@pytest.fixture(scope="module")
def launched(proxy: Proxy) -> Iterator[dict[str, Any]]:
    launch = _launch(proxy)
    try:
        yield launch
    finally:
        request(
            proxy.cluster,
            proxy.pod,
            "DELETE",
            launch_url(FIRST, "demo"),
            spawner_token(proxy.cluster),
        )


def test_a_launch_gives_the_gateway_a_key_that_only_lists_models(
    proxy: Proxy, launched: dict[str, Any]
) -> None:
    discovery = secret(
        proxy.cluster, SESSIONS, launched["gateway_service_account"], "model-discovery-key"
    )
    status, listed = proxy.call(discovery, "GET", "/v1/models")
    assert status == 200, listed
    assert SYNTHETIC in {item["id"] for item in listed["data"]}
    assert proxy.chat(discovery)[0] == 403
    assert proxy.call(discovery, "GET", "/key/info")[0] == 403
    # The worker key is in the session's OpenShell provider, not in the gateway's Secret.
    result = proxy.cluster.kubectl(
        "get",
        f"--namespace={SESSIONS}",
        "secret",
        launched["gateway_service_account"],
        "--output=json",
    )
    data = json.loads(result.stdout)["data"]
    keys = [
        name for name, value in data.items() if base64.b64decode(value).decode().startswith("sk-")
    ]
    assert keys == ["model-discovery-key"]


def test_a_relaunch_replaces_the_discovery_key(proxy: Proxy, launched: dict[str, Any]) -> None:
    account = launched["gateway_service_account"]
    first = secret(proxy.cluster, SESSIONS, account, "model-discovery-key")

    def relaunch() -> str:
        _launch(proxy)
        return secret(proxy.cluster, SESSIONS, account, "model-discovery-key")

    second = relaunch()
    assert second != first
    # A gateway started before the launch keeps its key until the next launch.
    assert proxy.call(first, "GET", "/v1/models")[0] == 200
    third = relaunch()
    assert proxy.call(first, "GET", "/v1/models")[0] == 401
    assert proxy.call(second, "GET", "/v1/models")[0] == 200
    assert proxy.call(third, "GET", "/v1/models")[0] == 200


def test_a_deletion_settles_and_revokes_the_keys(proxy: Proxy, launched: dict[str, Any]) -> None:
    account = launched["gateway_service_account"]
    discovery = secret(proxy.cluster, SESSIONS, account, "model-discovery-key")
    status, _ = request(
        proxy.cluster, proxy.pod, "DELETE", launch_url(FIRST, "demo"), spawner_token(proxy.cluster)
    )
    assert status == 204
    assert proxy.call(discovery, "GET", "/v1/models")[0] == 401
    # Deleting a key that is gone succeeds, so a repeated settlement completes.
    status, answer = proxy.call(proxy.master, "POST", "/key/delete", {"keys": [_hash(discovery)]})
    assert status in {200, 404}, answer
