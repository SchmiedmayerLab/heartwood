# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The data service, the object store and project data volumes on the cluster.

A synthetic researcher with sessions in both projects resolves catalog objects through
the DRS endpoints with tokens the project clients exchanged for the data service. Each
launch fills the session's project data volume through a copy job, whose pods alone
reach the object store from a session namespace.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from heartwood_platform.data.signing import Credentials, presign
from identity import (
    FIRST,
    PLATFORM,
    SESSIONS,
    exchange,
    launch_url,
    probe,
    request,
    secret,
    sign_in,
    spawner_token,
)
from identity import (
    researcher as researcher_id,
)
from kube import CONTROL, Cluster
from test_estimates import Researcher

DATA = "https://heartwood-data.heartwood-platform.svc.cluster.local:8443"
_DRS = f"{DATA}/ga4gh/drs/v1"
_STORE = "https://versitygw.heartwood-platform.svc.cluster.local:7070"
_DATASETS = (
    Path(__file__).resolve().parents[2] / "chart" / "heartwood-platform" / "files" / "datasets"
)
_COPY_ROLE = {"heartwood.platform/role": "project-data-copy"}


@pytest.fixture(scope="module")
def researcher(cluster: Cluster, probe_image: str) -> Iterator[Researcher]:
    """The first researcher, with a session and tokens in both projects."""
    with probe(cluster, "hw-data-probe", probe_image) as pod:
        found = Researcher(cluster, pod)
        try:
            for project in ("demo", "other"):
                found.launch(project)
            yield found
        finally:
            for project in ("demo", "other"):
                request(cluster, pod, "DELETE", launch_url(FIRST, project), spawner_token(cluster))


def _sha256(path: str) -> str:
    return hashlib.sha256((_DATASETS / path).read_bytes()).hexdigest()


def _json(cluster: Cluster, *arguments: str) -> Any:  # noqa: ANN401
    result = cluster.kubectl("get", *arguments, "--output=json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@contextmanager
def _session_pod(
    cluster: Cluster,
    namespace: str,
    image: str,
    labels: dict[str, str],
    claim: str | None = None,
) -> Iterator[str]:
    """Run a pod in a session namespace, with the platform CA and optionally a claim."""
    name = f"hw-data-probe-{secrets.token_hex(4)}"
    mounts: list[dict[str, Any]] = [{"name": "platform-ca", "mountPath": "/etc/platform-ca"}]
    volumes: list[dict[str, Any]] = [{"name": "platform-ca", "configMap": {"name": "platform-ca"}}]
    if claim is not None:
        mounts.append({"name": "data", "mountPath": "/data", "readOnly": True})
        volumes.append(
            {"name": "data", "persistentVolumeClaim": {"claimName": claim, "readOnly": True}}
        )
    limits = {"cpu": "100m", "memory": "64Mi"}
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "automountServiceAccountToken": False,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 10001,
                    "runAsGroup": 10001,
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
                        "resources": {"limits": limits, "requests": limits},
                        "volumeMounts": mounts,
                    }
                ],
                "volumes": volumes,
            },
        }
    )
    try:
        ready = cluster.kubectl(
            "wait", f"--namespace={namespace}", "--for=condition=Ready", f"pod/{name}"
        )
        assert ready.returncode == 0, ready.stderr
        yield name
    finally:
        cluster.kubectl("delete", f"--namespace={namespace}", f"pod/{name}", "--wait")


@contextmanager
def _copy_pod(cluster: Cluster, namespace: str, image: str) -> Iterator[str]:
    """Run a pod with the copy label as the control service does: through a copy Job.

    The admission policy admits the label only on pods that the Job controller creates
    for the control service's copy Jobs, so the probe is such a Job's pod.
    """
    name = f"hw-project-data-copy-probe-{secrets.token_hex(4)}"
    limits = {"cpu": "100m", "memory": "64Mi"}
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": _COPY_ROLE},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": _COPY_ROLE},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
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
                            "resources": {"limits": limits, "requests": limits},
                            "volumeMounts": [
                                {"name": "platform-ca", "mountPath": "/etc/platform-ca"}
                            ],
                        }
                    ],
                    "volumes": [{"name": "platform-ca", "configMap": {"name": "platform-ca"}}],
                },
            },
        },
    }
    created = cluster.kubectl("create", "--filename", "-", actor=CONTROL, document=job)
    assert created.returncode == 0, created.stderr
    try:
        # The Job controller creates the pod shortly after the Job.
        for _ in range(60):
            listed = _json(
                cluster, f"--namespace={namespace}", "pods", f"--selector=job-name={name}"
            )
            if listed["items"]:
                break
            time.sleep(1)
        ready = cluster.kubectl(
            "wait",
            f"--namespace={namespace}",
            "--for=condition=Ready",
            "pod",
            f"--selector=job-name={name}",
            "--timeout=120s",
        )
        assert ready.returncode == 0, ready.stderr
        pod = _json(cluster, f"--namespace={namespace}", "pods", f"--selector=job-name={name}")
        (item,) = pod["items"]
        yield str(item["metadata"]["name"])
    finally:
        cluster.kubectl("delete", f"--namespace={namespace}", f"job/{name}", "--wait")


def _run(cluster: Cluster, namespace: str, pod: str, *command: str) -> tuple[int, str]:
    result = cluster.kubectl("exec", f"--namespace={namespace}", pod, "--", *command)
    return result.returncode, result.stdout + result.stderr


def _fetch(cluster: Cluster, namespace: str, pod: str, url: str) -> tuple[int, str]:
    """Fetch a URL from a pod by the object store's address, so DNS plays no part."""
    address = _json(cluster, f"--namespace={PLATFORM}", "service", "versitygw")["spec"]["clusterIP"]
    return _run(
        cluster,
        namespace,
        pod,
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        "10",
        "--cacert",
        "/etc/platform-ca/ca.crt",
        "--resolve",
        f"versitygw.heartwood-platform.svc.cluster.local:7070:{address}",
        "--write-out",
        "\n%{http_code}",
        url,
    )


def _object(researcher: Researcher, token: str | None, identifier: str) -> tuple[int, Any]:
    return request(researcher.cluster, researcher.pod, "GET", f"{_DRS}/objects/{identifier}", token)


def test_service_info_is_served_only_over_https(researcher: Researcher) -> None:
    status, info = request(researcher.cluster, researcher.pod, "GET", f"{_DRS}/service-info")
    assert status == 200
    assert info["type"] == {"group": "org.ga4gh", "artifact": "drs", "version": "1.5.0"}
    plain = researcher.cluster.kubectl(
        "exec",
        f"--namespace={SESSIONS}",
        researcher.pod,
        "--",
        "curl",
        "--silent",
        "--max-time",
        "10",
        "http://heartwood-data.heartwood-platform.svc.cluster.local:8443/healthz",
    )
    assert plain.returncode != 0
    assert not plain.stdout


def test_objects_resolve_only_in_the_project_of_the_token(researcher: Researcher) -> None:
    demo, other = researcher.tokens["demo"]["data"], researcher.tokens["other"]["data"]
    status, found = _object(researcher, demo, "synthetic-cohort.participants.csv")
    assert status == 200, found
    assert found["checksums"] == [
        {"checksum": _sha256("synthetic-cohort/participants.csv"), "type": "sha-256"}
    ]
    assert found["self_uri"] == (
        "drs://heartwood-data.heartwood-platform.svc.cluster.local:8443/"
        "synthetic-cohort.participants.csv"
    )
    # The researcher is a member of both projects; the token of project demo does not
    # reach project other's objects, and the reverse holds too.
    assert _object(researcher, demo, "synthetic-panel.panel.csv")[0] == 404
    assert _object(researcher, other, "synthetic-cohort.participants.csv")[0] == 404
    assert _object(researcher, other, "synthetic-panel.panel.csv")[0] == 200
    # A token for another service, and no token, are refused.
    control = researcher.tokens["demo"]["control"]
    assert _object(researcher, control, "synthetic-cohort.participants.csv")[0] == 401
    assert _object(researcher, None, "synthetic-cohort.participants.csv")[0] == 401


def test_a_researcher_outside_the_project_is_refused(researcher: Researcher) -> None:
    # The second researcher signs in at project other's client, which Keycloak allows;
    # the data service refuses, because the registry does not list her as a member.
    client_secret = secret(
        researcher.cluster,
        SESSIONS,
        f"hw-gw-{researcher.sessions['other']}",
        "keycloak-client-secret",
    )
    redirect = (
        "http://proxy-public.heartwood-platform.svc.cluster.local"
        f"/user/{researcher_id(FIRST)}/other/oauth_callback"
    )
    signed_in = sign_in(
        researcher.cluster,
        researcher.pod,
        username="researcher-two",
        redirect_uri=redirect,
        client_id="hw-proj-other",
        client_secret=client_secret,
    )
    status, exchanged = exchange(
        researcher.cluster,
        researcher.pod,
        subject_token=signed_in["access_token"],
        service="data",
        audience="heartwood-data",
        client_id="hw-proj-other",
        client_secret=client_secret,
    )
    assert status == 200, exchanged
    token = exchanged["access_token"]
    assert _object(researcher, token, "synthetic-panel.panel.csv")[0] == 403


def test_object_urls_read_one_object_from_copy_pods_only(
    researcher: Researcher, probe_image: str
) -> None:
    cluster = researcher.cluster
    namespace = f"hw-s-{researcher.sessions['demo']}"
    status, access = request(
        cluster,
        researcher.pod,
        "GET",
        f"{_DRS}/objects/synthetic-cohort.measurements.csv/access/https",
        researcher.tokens["demo"]["data"],
    )
    assert status == 200, access
    url = access["url"]
    assert "X-Amz-Expires=301" in url
    with _copy_pod(cluster, namespace, probe_image) as pod:
        code, output = _fetch(cluster, namespace, pod, url)
        body, _, http = output.rpartition("\n")
        assert (code, http) == (0, "200"), output
        assert hashlib.sha256(body.encode()).hexdigest() == _sha256(
            "synthetic-cohort/measurements.csv"
        )
        # The URL names one object; the same signature does not read another.
        other = url.replace("measurements.csv", "participants.csv")
        assert _fetch(cluster, namespace, pod, other)[1].endswith("\n403")
        # The data service's account reads and never writes, and its URLs expire.
        data = Credentials(
            secret(cluster, PLATFORM, "versitygw-data", "access-key"),
            secret(cluster, PLATFORM, "versitygw-data", "secret-key"),
        )
        write = presign(
            method="PUT",
            endpoint=_STORE,
            path="/hw-project-demo/datasets/synthetic-cohort/written.csv",
            credentials=data,
            region="us-east-1",
            expires=60,
            now=datetime.now(UTC),
        )
        code, output = _run(
            cluster,
            namespace,
            pod,
            "curl",
            "--silent",
            "--max-time",
            "10",
            "--cacert",
            "/etc/platform-ca/ca.crt",
            "--request",
            "PUT",
            "--data-binary",
            "x",
            "--write-out",
            "\n%{http_code}",
            write,
        )
        assert output.endswith("\n403"), output
        listing = presign(
            method="GET",
            endpoint=_STORE,
            path="/hw-project-demo",
            credentials=data,
            region="us-east-1",
            expires=60,
            now=datetime.now(UTC),
        )
        assert _fetch(cluster, namespace, pod, listing)[1].endswith("\n403")
        brief = presign(
            method="GET",
            endpoint=_STORE,
            path="/hw-project-demo/datasets/synthetic-cohort/README.md",
            credentials=data,
            region="us-east-1",
            expires=2,
            now=datetime.now(UTC),
        )
        assert _fetch(cluster, namespace, pod, brief)[1].endswith("\n200")
        time.sleep(4)
        expired = _fetch(cluster, namespace, pod, brief)[1]
        assert expired.endswith("\n403")
        assert "expired" in expired
        # A copy pod reaches nothing else, such as Keycloak.
        code, _ = _run(
            cluster,
            namespace,
            pod,
            "curl",
            "--silent",
            "--max-time",
            "10",
            "--cacert",
            "/etc/platform-ca/ca.crt",
            "https://keycloak.heartwood-platform.svc.cluster.local:8443/health",
        )
        assert code != 0
    # Pods without the copy label, in the session namespace and in heartwood-sessions,
    # do not reach the object store.
    with _session_pod(cluster, namespace, probe_image, {}) as pod:
        code, output = _fetch(cluster, namespace, pod, url)
        assert code != 0, output
    code = curl_exit(cluster, researcher.pod, url)
    assert code != 0


def curl_exit(cluster: Cluster, pod: str, url: str) -> int:
    result = cluster.kubectl(
        "exec",
        f"--namespace={SESSIONS}",
        pod,
        "--",
        "curl",
        "--silent",
        "--max-time",
        "10",
        "--cacert",
        "/etc/platform-ca/ca.crt",
        url,
    )
    return result.returncode


@pytest.mark.parametrize("project", ["demo", "other"])
def test_a_launch_fills_an_attachable_volume_with_the_projects_objects(
    researcher: Researcher, probe_image: str, project: str
) -> None:
    cluster = researcher.cluster
    namespace = f"hw-s-{researcher.sessions[project]}"
    launch = json.loads(
        secret(cluster, SESSIONS, f"hw-gw-{researcher.sessions[project]}", "launch.json")
    )
    volume = launch["project_data"]
    claim = _json(
        cluster, f"--namespace={namespace}", "persistentvolumeclaim", volume["claim_name"]
    )
    assert claim["metadata"]["uid"] == volume["claim_uid"]
    assert claim["metadata"]["labels"]["heartwood.platform/project"] == project
    assert claim["metadata"]["labels"]["openshell.ai/sandbox-attachable"] == "true"
    assert claim["metadata"]["labels"]["openshell.ai/sandbox-attachable-workspace"] == "research"
    assert (
        claim["metadata"]["annotations"]["heartwood.platform/content-sha256"]
        == volume["content_sha256"]
    )
    # The copy job and its plan, which held the URLs, are gone.
    jobs = _json(cluster, f"--namespace={namespace}", "jobs")["items"]
    assert not [job for job in jobs if job["metadata"]["name"].startswith("hw-project-data")]
    secrets_left = _json(cluster, f"--namespace={namespace}", "secrets")["items"]
    assert not [s for s in secrets_left if s["metadata"]["name"].startswith("hw-project-data")]
    files = {
        "demo": [
            "synthetic-cohort/README.md",
            "synthetic-cohort/participants.csv",
            "synthetic-cohort/measurements.csv",
        ],
        "other": ["synthetic-panel/panel.csv"],
    }[project]
    assert volume["objects"] == len(files)
    with _session_pod(cluster, namespace, probe_image, {}, claim=volume["claim_name"]) as pod:
        code, output = _run(cluster, namespace, pod, "sha256sum", *(f"/data/{f}" for f in files))
        assert code == 0, output
        assert {
            line.split()[1].removeprefix("/data/"): line.split()[0] for line in output.splitlines()
        } == {f: _sha256(f) for f in files}
        code, output = _run(cluster, namespace, pod, "sh", "-c", "echo x > /data/written")
        assert code != 0
        assert "Read-only file system" in output
        code, output = _run(cluster, namespace, pod, "ls", "/data")
        assert output.split() == [files[0].split("/")[0]]


def test_a_second_launch_reuses_the_filled_volume(researcher: Researcher) -> None:
    cluster = researcher.cluster
    before = json.loads(
        secret(cluster, SESSIONS, f"hw-gw-{researcher.sessions['demo']}", "launch.json")
    )["project_data"]
    researcher.launch("demo")
    after = json.loads(
        secret(cluster, SESSIONS, f"hw-gw-{researcher.sessions['demo']}", "launch.json")
    )["project_data"]
    assert after == before


def test_only_copy_jobs_carry_the_copy_label(researcher: Researcher, probe_image: str) -> None:
    cluster = researcher.cluster
    namespace = f"hw-s-{researcher.sessions['demo']}"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "hw-copy-label-probe", "namespace": namespace, "labels": _COPY_ROLE},
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [
                {
                    "name": "probe",
                    "image": probe_image,
                    "securityContext": {
                        "runAsUser": 10001,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}},
                }
            ],
        },
    }
    # The OpenShell gateway creates pods in the session namespace, and the copy label is
    # refused to it, as to the cluster administrator.
    for actor in (f"system:serviceaccount:{namespace}:openshell", None):
        result = cluster.kubectl(
            "create", "--dry-run=server", "--filename", "-", actor=actor, document=pod
        )
        assert result.returncode != 0
        assert "copy Jobs carry the project data copy label" in result.stderr, result.stderr


def test_a_running_copy_pod_cannot_be_changed(researcher: Researcher, probe_image: str) -> None:
    cluster = researcher.cluster
    namespace = f"hw-s-{researcher.sessions['demo']}"
    with _copy_pod(cluster, namespace, probe_image) as pod:
        patch = json.dumps({"spec": {"containers": [{"name": "agnhost", "image": probe_image}]}})
        allowed = cluster.kubectl(
            "patch", f"--namespace={namespace}", f"pod/{pod}", "--dry-run=server", "--patch", patch
        )
        assert allowed.returncode == 0, allowed.stderr
        other = probe_image.replace("@sha256:", ":other@sha256:")
        changed = json.dumps({"spec": {"containers": [{"name": "agnhost", "image": other}]}})
        refused = cluster.kubectl(
            "patch",
            f"--namespace={namespace}",
            f"pod/{pod}",
            "--dry-run=server",
            "--patch",
            changed,
            actor=f"system:serviceaccount:{namespace}:openshell",
        )
        assert refused.returncode != 0
        assert "copy Jobs carry the project data copy label" in refused.stderr, refused.stderr


def _tool(researcher: Researcher, token: str | None, name: str, **arguments: str) -> Any:  # noqa: ANN401
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    status, answer = request(researcher.cluster, researcher.pod, "POST", f"{DATA}/mcp", token, body)
    assert status == 200, answer
    return answer["result"]


def test_the_catalog_tools_answer_for_the_session_of_the_token(researcher: Researcher) -> None:
    token = researcher.tokens["demo"]["data"]
    operation = str(uuid4())
    listed = _tool(researcher, token, "list_datasets", project_id="demo", operation_id=operation)
    assert listed["isError"] is False, listed
    observation = listed["structuredContent"]
    assert observation["project_context_id"] == researcher.sessions["demo"]
    assert observation["status"] == "completed"
    (dataset,) = observation["result"]["datasets"]
    assert dataset["id"] == "synthetic-cohort"
    # The kept observation answers the same identifier, and another request is refused.
    kept = _tool(researcher, token, "get_operation", project_id="demo", operation_id=operation)
    assert kept["structuredContent"] == observation
    conflict = _tool(
        researcher,
        token,
        "resolve_object",
        object_id="synthetic-cohort.README.md",
        project_id="demo",
        operation_id=operation,
    )
    assert conflict["isError"] is True
    denied = _tool(
        researcher,
        token,
        "resolve_object",
        object_id="synthetic-panel.panel.csv",
        project_id="demo",
        operation_id=str(uuid4()),
    )
    assert denied["structuredContent"]["status"] == "denied"
    # The token of project demo cannot name project other.
    other = _tool(researcher, token, "list_datasets", project_id="other", operation_id=str(uuid4()))
    assert other["isError"] is True
    status, _ = request(
        researcher.cluster,
        researcher.pod,
        "POST",
        f"{DATA}/mcp",
        researcher.tokens["demo"]["control"],
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert status == 401
