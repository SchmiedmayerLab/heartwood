# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A researcher's gateway started by JupyterHub, and what the Hub may not do.

The tests run in order and share one named server: the first researcher signs in to the
Hub, starts the server of project demo, signs in to the gateway behind the Hub's proxy
and plans against the control service. The second researcher is refused. The idle
culler stops the server, and a new start recovers its session. The server is deleted
at the end.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hub import ORIGIN, Browser, gateway_pods, wait_for_gateway
from identity import FIRST, PLATFORM, SECOND, SESSIONS, probe
from kube import Cluster

_HUB = f"system:serviceaccount:{PLATFORM}:hub"
_BASE = f"/user/{FIRST}/demo"
_PLAN = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "synthetic"
    / "platform-contract"
    / "research-plan.r1.prediction.json"
)


@dataclass
class Server:
    """The first researcher's named server of project demo."""

    first: Browser
    second: Browser
    session: str = ""
    capability: str = ""
    recorded: dict[str, Any] = field(default_factory=dict)


def _json(cluster: Cluster, *arguments: str) -> Any:  # noqa: ANN401
    result = cluster.kubectl("get", *arguments, "--output=json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _start(server: Server) -> None:
    status, body = server.first.spawn(FIRST, "demo")
    assert status == 302, body[:300]
    wait_for_gateway(server.first, _BASE)
    (pod,) = gateway_pods(server.first.cluster)
    server.session = pod["metadata"]["labels"]["heartwood.platform/session"]


@pytest.fixture(scope="module")
def server(cluster: Cluster, probe_image: str) -> Iterator[Server]:
    with (
        probe(cluster, "hw-hub-first", probe_image) as first,
        probe(cluster, "hw-hub-second", probe_image) as second,
    ):
        state = Server(
            Browser(cluster, first, "researcher-one"), Browser(cluster, second, "researcher-two")
        )
        try:
            yield state
        finally:
            state.first.hub_api("DELETE", f"/users/{FIRST}/servers/demo", {"remove": True})


def test_a_researcher_signs_in_to_the_hub_through_keycloak(server: Server) -> None:
    server.first.sign_in_to_hub()
    status, user = server.first.hub_api("GET", f"/users/{FIRST}")
    assert status == 200, user
    assert user["name"] == FIRST


def test_the_hub_offers_only_the_researchers_projects(server: Server) -> None:
    status, _, page = server.first.get(f"/hub/spawn/{FIRST}/demo")
    assert status == 200
    assert 'value="demo"' in page
    status, _, page = server.first.get(f"/hub/spawn/{FIRST}/unknown")
    assert 'value="unknown"' not in page


def test_a_named_server_starts_the_gateway_in_a_provisioned_session(
    server: Server, cluster: Cluster
) -> None:
    _start(server)
    (pod,) = gateway_pods(cluster, server.session)
    spec = pod["spec"]
    assert spec["serviceAccountName"] == f"hw-gw-{server.session}"
    assert pod["metadata"]["namespace"] == SESSIONS
    namespace = _json(cluster, "namespace", f"hw-s-{server.session}")
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    # The gateway checked its OpenShell gateway's deletion capabilities before serving.
    log = cluster.kubectl(
        "logs", f"--namespace={SESSIONS}", pod["metadata"]["name"], "-c", "notebook"
    )
    assert "Heartwood is served at" in log.stdout, log.stdout[-2000:]
    assert "HW-WORKER-001" not in log.stdout
    # Only the init container mounts the session's Secret.
    (secret,) = [volume["name"] for volume in spec["volumes"] if "secret" in volume]
    assert secret not in {mount["name"] for mount in spec["containers"][0]["volumeMounts"]}


def test_the_researcher_opens_the_gateway_through_the_hubs_proxy(server: Server) -> None:
    server.capability = server.first.launch(_BASE)
    status, _, page = server.first.get(f"{_BASE}/")
    assert status == 200
    assert "<title>" in page
    status, created = server.first.api(
        _BASE, "POST", "/sessions", server.capability, {"title": "Hub"}
    )
    assert status == 201, created
    server.recorded = created
    status, sessions = server.first.api(_BASE, "GET", "/sessions", server.capability)
    assert status == 200, sessions


def test_the_gateway_plans_with_an_exchanged_token_over_tls(server: Server) -> None:
    status, projection = server.first.api(
        _BASE, "POST", "/project/planning/refresh", server.capability
    )
    assert status == 200, projection
    assert projection["services"]["estimates"] == "available", projection
    prediction = json.loads(_PLAN.read_text(encoding="utf-8"))
    prediction["project_context_id"] = server.session
    for evidence in prediction["evidence"]:
        evidence["project_context_id"] = server.session
    status, projection = server.first.api(
        _BASE, "POST", "/project/planning/predictions", server.capability, prediction
    )
    assert status == 200, projection
    request = {"prediction": prediction["prediction"], "scope": "plan", "element_ids": []}
    status, projection = server.first.api(
        _BASE, "POST", "/project/planning/estimates", server.capability, request
    )
    assert status == 200, projection
    (plan,) = projection["plans"]
    # A server that ran before holds earlier estimates of the same revision.
    revision = plan["revisions"][0]
    assert revision["estimates"][-1]["state"] == "current", revision
    # The platform's estimate, priced from the chart's synthetic price sheet.
    estimate = revision["overview"]["estimates"][-1]
    assert estimate["project_context_id"] == server.session


def test_a_second_researcher_is_refused(server: Server, cluster: Cluster) -> None:
    second = server.second
    second.sign_in_to_hub()
    # Not a member of project other: the Hub offers it no server and refuses to start one.
    status, page = second.spawn(SECOND, "other")
    assert 'value="other"' not in page
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status, user = second.hub_api("GET", f"/users/{SECOND}")
        assert status == 200, user
        started = user["servers"].get("other")
        if started is None or not started["pending"]:
            break
        time.sleep(3)
    assert started is None or not started["ready"], started
    # The control service prepared no session for the refused start.
    secrets = _json(cluster, "secret", f"--namespace={SESSIONS}")["items"]
    names = {item["metadata"]["name"] for item in secrets}
    assert {name for name in names if name.startswith("hw-gw-")} == {f"hw-gw-{server.session}"}
    sessions = {
        pod["metadata"]["labels"]["heartwood.platform/session"] for pod in gateway_pods(cluster)
    }
    assert sessions == {server.session}
    # The first researcher's server through the Hub's API.
    status, _ = second.hub_api("POST", f"/users/{FIRST}/servers/other")
    # The Hub answers 404 for a user whom the caller may not see.
    assert status in {403, 404}
    # The first researcher's gateway, signed in as the second researcher.
    status, _, body = second.sign_in_to_gateway(_BASE)
    assert status == 403
    assert "HW-INGRESS-004" in body
    status, answer = second.api(_BASE, "GET", "/sessions", None)
    assert status == 401, answer


def test_the_hub_cannot_read_secrets_in_the_sessions_namespace(
    server: Server, cluster: Cluster
) -> None:
    for verb in ("get", "list", "watch"):
        result = cluster.kubectl(
            "auth", "can-i", verb, "secrets", f"--namespace={SESSIONS}", f"--as={_HUB}"
        )
        assert result.stdout.strip() == "no", (verb, result.stdout)
    result = cluster.kubectl(
        "get", f"--namespace={SESSIONS}", "secret", f"hw-gw-{server.session}", f"--as={_HUB}"
    )
    assert result.returncode != 0
    assert "Forbidden" in result.stderr
    assert 'cannot get resource "secrets"' in result.stderr


def _as_hub(cluster: Cluster, document: dict[str, Any]) -> tuple[int, str]:
    result = cluster.kubectl(
        "create", "--dry-run=server", f"--as={_HUB}", "--filename", "-", document=document
    )
    return result.returncode, result.stderr


def _pod_as_hub(cluster: Cluster, pod: dict[str, Any]) -> str:
    returncode, refusal = _as_hub(cluster, pod)
    assert returncode != 0, "the pod was admitted"
    return refusal


def test_the_hub_creates_only_gateway_pods_of_the_admitted_shape(
    server: Server, cluster: Cluster
) -> None:
    (running,) = gateway_pods(cluster, server.session)
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {}, "spec": running["spec"]}
    pod["metadata"] = {
        "name": "hw-hub-shape",
        "namespace": SESSIONS,
        "labels": running["metadata"]["labels"],
    }
    for field_name in ("nodeName", "volumes"):
        pod["spec"] = {key: value for key, value in pod["spec"].items() if key != field_name}
    pod["spec"]["volumes"] = [
        volume for volume in running["spec"]["volumes"] if not volume["name"].startswith("kube-api")
    ]
    containers = [
        {key: value for key, value in container.items() if key not in {"volumeMounts"}}
        | {
            "volumeMounts": [
                m for m in container["volumeMounts"] if not m["name"].startswith("kube-api")
            ]
        }
        for container in running["spec"]["containers"]
    ]
    init = [
        {key: value for key, value in container.items() if key != "volumeMounts"}
        | {
            "volumeMounts": [
                m for m in container["volumeMounts"] if not m["name"].startswith("kube-api")
            ]
        }
        for container in running["spec"]["initContainers"]
    ]

    def changed(**spec: Any) -> dict[str, Any]:  # noqa: ANN401
        return {
            **pod,
            "spec": {**pod["spec"], "containers": containers, "initContainers": init, **spec},
        }

    other_image = [{**containers[0], "image": "registry.k8s.io/pause:3.10.1"}]
    shell = [{**containers[0], "args": ["sh", "-c", "cat /etc/heartwood/deployment/client-secret"]}]
    mounts = [*containers[0]["volumeMounts"], {"name": "source", "mountPath": "/source"}]
    other_secret = [
        volume
        if volume["name"] != "source"
        else {"name": "source", "secret": {"secretName": "hw-gw-0000000000000000"}}
        for volume in pod["spec"]["volumes"]
    ]
    probe_exec = [{**containers[0], "livenessProbe": {"exec": {"command": ["true"]}}}]
    # The unchanged shape, as the Hub created it, is admitted.
    assert _as_hub(cluster, changed()) == (0, "")
    secret_env = [
        {
            **containers[0],
            "env": [
                *containers[0]["env"],
                {
                    "name": "JUPYTERHUB_LEAK",
                    "valueFrom": {"secretKeyRef": {"name": f"hw-gw-{server.session}", "key": "x"}},
                },
            ],
        }
    ]
    command = [{**containers[0], "command": ["sh", "-c", "env"]}]
    hook = [{**containers[0], "lifecycle": {"postStart": {"exec": {"command": ["true"]}}}}]
    message_file = [
        {**containers[0], "terminationMessagePath": "/etc/heartwood/deployment/client-secret"}
    ]
    # The memory volume with the copies of the Secret at the research directory.
    research = [
        {
            **containers[0],
            "volumeMounts": [
                {**mount, "name": "work", "subPath": "deployment"}
                if mount["mountPath"] == "/research"
                else mount
                for mount in containers[0]["volumeMounts"]
            ],
        }
    ]
    # A volume named like the ServiceAccount token volume that carries the Secret.
    token_like = [
        *pod["spec"]["volumes"],
        {
            "name": "kube-api-access-x",
            "projected": {"sources": [{"secret": {"name": f"hw-gw-{server.session}"}}]},
        },
    ]
    annotated = {**pod["metadata"], "annotations": {"k8s.v1.cni.cncf.io/networks": "other"}}
    # The init container writing the copies onto the server's volume.
    persisted = [
        {
            **init[0],
            "volumeMounts": [
                {**mount, "name": "home"} if mount["mountPath"] == "/etc/heartwood/work" else mount
                for mount in init[0]["volumeMounts"]
            ],
        }
    ]
    sidecar = [{**init[0], "restartPolicy": "Always"}]
    labels = {**pod["metadata"]["labels"], "app.kubernetes.io/component": "hub"}
    cases = [
        ("hosted gateway image", changed(containers=other_image)),
        ("fixed arguments", changed(containers=shell)),
        # The Secret in the container.
        (
            "exactly the volumes and paths",
            changed(containers=[{**containers[0], "volumeMounts": mounts}]),
        ),
        ("mounts only its session's Secret", changed(volumes=other_secret)),
        ("hw-gw-<session>", changed(serviceAccountName="default")),
        # The restricted Pod Security Standard of the namespace refuses it first.
        ("PodSecurity", changed(hostNetwork=True)),
        ("no exec probe", changed(containers=probe_exec)),
        ("only the Hub's environment variables", changed(containers=secret_env)),
        ("fixed arguments", changed(containers=command)),
        ("no lifecycle hook", changed(containers=hook)),
        ("default termination message", changed(containers=message_file)),
        ("exactly the volumes and paths", changed(containers=research)),
        ("exactly the volumes and paths", changed(initContainers=persisted)),
        ("configuration script", changed(initContainers=sidecar)),
        ("cluster's DNS", changed(hostAliases=[{"ip": "10.0.0.1", "hostnames": ["keycloak"]}])),
        ("its ServiceAccount token", changed(volumes=token_like)),
        ("only annotations of JupyterHub", {**changed(), "metadata": annotated}),
        (
            "only the labels",
            {**changed(), "metadata": {**pod["metadata"], "labels": labels}},
        ),
    ]
    for message, case in cases:
        # The pod specs hold the server's Hub token, so only the refusal is reported.
        refusal = _pod_as_hub(cluster, case)
        assert message in refusal, refusal


def test_the_hub_creates_only_its_own_volume_claims_and_nothing_elsewhere(
    server: Server, cluster: Cluster
) -> None:
    claim = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"hw-home-{server.session}x", "namespace": SESSIONS},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
            "dataSource": {"kind": "PersistentVolumeClaim", "name": f"hw-home-{server.session}"},
        },
    }
    returncode, refusal = _as_hub(cluster, claim)
    assert returncode != 0
    assert "name no data source" in refusal
    elsewhere = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "hw-hub-elsewhere", "namespace": f"hw-s-{server.session}"},
        "spec": {"containers": [{"name": "c", "image": "registry.k8s.io/pause:3.10.1"}]},
    }
    returncode, refusal = _as_hub(cluster, elsewhere)
    assert returncode != 0
    assert "Forbidden" in refusal


def test_the_idle_culler_stops_the_server_and_keeps_the_session(
    server: Server, cluster: Cluster
) -> None:
    deadline = time.monotonic() + 420
    while gateway_pods(cluster, server.session) and time.monotonic() < deadline:
        time.sleep(10)
    assert not gateway_pods(cluster, server.session), "the culler did not stop the server"
    # The session namespace, its OpenShell gateway and the server's volume stay.
    assert _json(cluster, "namespace", f"hw-s-{server.session}")
    claim = _json(
        cluster, "persistentvolumeclaim", f"--namespace={SESSIONS}", f"hw-home-{server.session}"
    )
    assert claim["status"]["phase"] == "Bound"


def test_a_restart_recovers_the_session(server: Server) -> None:
    session = server.session
    _start(server)
    assert server.session == session
    # Tokens live in memory only, so the researcher signs in again after the restart.
    server.capability = server.first.launch(_BASE)
    status, sessions = server.first.api(_BASE, "GET", "/sessions", server.capability)
    assert status == 200, sessions
    listed = json.dumps(sessions)
    assert server.recorded["session_id"] in listed, (server.recorded, sessions)
    status, _, _ = server.first.get(f"{ORIGIN}{_BASE}/")
    assert status == 200


def test_deleting_the_named_server_removes_the_session(server: Server, cluster: Cluster) -> None:
    status, answer = server.first.hub_api(
        "DELETE", f"/users/{FIRST}/servers/demo", {"remove": True}
    )
    assert status in {202, 204}, answer
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        namespace = cluster.kubectl("get", "namespace", f"hw-s-{server.session}")
        if namespace.returncode != 0:
            break
        time.sleep(5)
    assert cluster.kubectl("get", "namespace", f"hw-s-{server.session}").returncode != 0
    for kind in ("secret", "serviceaccount", "persistentvolumeclaim"):
        name = (
            f"hw-home-{server.session}"
            if kind == "persistentvolumeclaim"
            else f"hw-gw-{server.session}"
        )
        result = cluster.kubectl("get", f"--namespace={SESSIONS}", kind, name)
        assert result.returncode != 0, (kind, result.stdout)
