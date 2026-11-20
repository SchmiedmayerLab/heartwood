# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Recovery of a hosted session and rotation of its project client secret.

The tests run in order and share the first researcher's named server of project
other. The gateway pod fails, the Hub restarts and the control service restarts, and
each time the session continues from its retained state. An operator rotates the
project's client secret: Keycloak refuses the previous secret, and the next start of the
server uses the new one.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from hub import ORIGIN, Browser, gateway_pods, wait_for_gateway
from identity import (
    CONTROL,
    FIRST,
    PLATFORM,
    SESSIONS,
    exchange,
    probe,
    request,
    secret,
    sign_in,
)
from kube import Cluster

_BASE = f"/user/{FIRST}/other"
_OPERATOR = f"system:serviceaccount:{PLATFORM}:heartwood-operator"


@dataclass
class Server:
    """The first researcher's named server of project other."""

    browser: Browser
    pod: str
    session: str = ""
    recorded: str = ""
    capability: str = ""


def _start(server: Server) -> None:
    status, body = server.browser.spawn(FIRST, "other")
    assert status == 302, body[:300]
    wait_for_gateway(server.browser, _BASE)
    (pod,) = gateway_pods(server.browser.cluster, server.session or None)
    server.session = pod["metadata"]["labels"]["heartwood.platform/session"]


def _stop(server: Server) -> None:
    server.browser.hub_api("DELETE", f"/users/{FIRST}/servers/other")
    deadline = time.monotonic() + 120
    while gateway_pods(server.browser.cluster, server.session) and time.monotonic() < deadline:
        time.sleep(3)
    assert not gateway_pods(server.browser.cluster, server.session)


def _sessions(server: Server) -> list[str]:
    status, sessions = server.browser.api(_BASE, "GET", "/sessions", server.capability)
    assert status == 200, sessions
    return [item["session_id"] for item in sessions["sessions"]]


def _planning(server: Server) -> str:
    status, projection = server.browser.api(
        _BASE, "POST", "/project/planning/refresh", server.capability
    )
    assert status == 200, projection
    estimates = projection["services"]["estimates"]
    assert isinstance(estimates, str)
    return estimates


@pytest.fixture(scope="module")
def server(cluster: Cluster, probe_image: str) -> Iterator[Server]:
    with probe(cluster, "hw-recovery-browser", probe_image) as pod:
        state = Server(Browser(cluster, pod, "researcher-one"), pod)
        state.browser.sign_in_to_hub()
        _start(state)
        state.capability = state.browser.launch(_BASE)
        status, created = state.browser.api(
            _BASE, "POST", "/sessions", state.capability, {"title": "Recovery"}
        )
        assert status == 201, created
        state.recorded = created["session_id"]
        try:
            yield state
        finally:
            state.browser.hub_api("DELETE", f"/users/{FIRST}/servers/other", {"remove": True})


def test_a_failed_gateway_pod_restarts_with_its_retained_state(
    server: Server, cluster: Cluster
) -> None:
    (pod,) = gateway_pods(cluster, server.session)
    result = cluster.kubectl(
        "delete",
        f"--namespace={SESSIONS}",
        "pod",
        pod["metadata"]["name"],
        "--grace-period=0",
        "--force",
    )
    assert result.returncode == 0, result.stderr
    # The Hub notices the stopped server; the researcher starts it again.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status, user = server.browser.hub_api("GET", f"/users/{FIRST}")
        if status == 200 and "other" not in user.get("servers", {}):
            break
        time.sleep(5)
    _start(server)
    server.capability = server.browser.launch(_BASE)
    assert server.recorded in _sessions(server)


def test_a_hub_restart_keeps_the_running_gateway(server: Server, cluster: Cluster) -> None:
    (before,) = gateway_pods(cluster, server.session)
    restarted = cluster.kubectl("rollout", "restart", f"--namespace={PLATFORM}", "deployment/hub")
    assert restarted.returncode == 0, restarted.stderr
    status = cluster.kubectl(
        "rollout", "status", f"--namespace={PLATFORM}", "deployment/hub", "--timeout=300s"
    )
    assert status.returncode == 0, status.stderr
    (after,) = gateway_pods(cluster, server.session)
    assert after["metadata"]["uid"] == before["metadata"]["uid"]
    # The proxy kept its route, and the gateway kept the researcher's sign-in.
    assert server.recorded in _sessions(server)


def test_a_control_service_restart_keeps_the_session(server: Server, cluster: Cluster) -> None:
    deleted = cluster.kubectl(
        "delete", f"--namespace={PLATFORM}", "pod", "heartwood-control-0", "--wait"
    )
    assert deleted.returncode == 0, deleted.stderr
    ready = cluster.kubectl(
        "wait",
        f"--namespace={PLATFORM}",
        "--for=condition=Ready",
        "pod/heartwood-control-0",
        "--timeout=300s",
    )
    assert ready.returncode == 0, ready.stderr
    _stop(server)
    session = server.session
    _start(server)
    # The registry on the service's volume returns the same session.
    assert server.session == session
    server.capability = server.browser.launch(_BASE)
    assert server.recorded in _sessions(server)
    assert _planning(server) == "available"


def _operator_token(cluster: Cluster) -> str:
    result = cluster.kubectl(
        "create",
        "token",
        "heartwood-operator",
        f"--namespace={PLATFORM}",
        "--audience=heartwood-control",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_an_operator_rotates_the_project_client_secret(server: Server, cluster: Cluster) -> None:
    secret = f"hw-gw-{server.session}"
    previous = _client_secret(cluster, secret)
    # The Hub's token is not an operator's.
    hub = cluster.kubectl(
        "create", "token", "hub", f"--namespace={PLATFORM}", "--audience=heartwood-control"
    ).stdout.strip()
    status, _ = request(
        cluster, server.pod, "POST", f"{CONTROL}/v1/projects/other/client-secret", hub
    )
    assert status == 401
    status, answer = request(
        cluster,
        server.pod,
        "POST",
        f"{CONTROL}/v1/projects/other/client-secret",
        _operator_token(cluster),
    )
    assert status == 200, answer
    assert server.session in answer["sessions"]
    current = _client_secret(cluster, secret)
    assert current != previous
    # Keycloak no longer accepts the previous secret.
    tokens = sign_in(
        cluster,
        server.pod,
        username="researcher-one",
        client_id="hw-proj-other",
        client_secret=current,
        redirect_uri=f"{ORIGIN}{_BASE}/oauth_callback",
    )
    for client_secret, expected in ((previous, 401), (current, 200)):
        status, _ = exchange(
            cluster,
            server.pod,
            client_id="hw-proj-other",
            client_secret=client_secret,
            subject_token=tokens["access_token"],
            service="control",
            audience="heartwood-control",
        )
        assert status == expected


def test_the_next_start_uses_the_rotated_secret(server: Server) -> None:
    _stop(server)
    _start(server)
    server.capability = server.browser.launch(_BASE)
    assert _planning(server) == "available"
    assert server.recorded in _sessions(server)


def _client_secret(cluster: Cluster, name: str) -> str:
    return secret(cluster, SESSIONS, name, "keycloak-client-secret")
