# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The job service and the Funnel executors on the cluster.

A synthetic researcher with sessions in both projects calls the job service's MCP tools
with tokens exchanged for it, from a probe pod in heartwood-sessions. Further tests check
the isolation of tasks, workers and projects that the chart sets up.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from heartwood_platform.jobs.tes import TesExecutorSpec, TesOutput, TesResources, TesTask
from identity import (
    FIRST,
    SESSIONS,
    exchange,
    launch_url,
    probe,
    request,
    secret,
    sign_in,
    spawner_token,
)
from jobs import (
    TASK_IMAGE,
    TERMINAL,
    executor,
    job_service_password,
    job_service_paused,
    store,
    wait_for,
)
from kube import Cluster

JOBS = "https://heartwood-jobs.heartwood-platform.svc.cluster.local:8443"
_BUCKET = "hw-project-demo"


class Researcher:
    """The first researcher, with a session and a job service token in each project."""

    def __init__(self, cluster: Cluster, pod: str) -> None:
        self.cluster = cluster
        self.pod = pod
        self.sessions: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        for project in ("demo", "other"):
            status, launch = request(
                cluster, pod, "PUT", launch_url(FIRST, project), spawner_token(cluster)
            )
            assert status == 200, launch
            client = {
                "client_id": launch["client_id"],
                "client_secret": secret(
                    cluster, SESSIONS, f"hw-gw-{launch['session']}", "keycloak-client-secret"
                ),
            }
            signed_in = sign_in(
                cluster,
                pod,
                username="researcher-one",
                redirect_uri=launch["redirect_uri"],
                **client,
            )
            status, exchanged = exchange(
                cluster,
                pod,
                subject_token=signed_in["access_token"],
                service="jobs",
                audience="heartwood-jobs",
                **client,
            )
            assert status == 200, exchanged.get("error_description")
            self.sessions[project] = launch["session"]
            self.tokens[project] = exchanged["access_token"]

    def call(
        self, project: str, tool: str, arguments: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        status, answer = request(
            self.cluster, self.pod, "POST", f"{JOBS}/mcp", token or self.tokens[project], body
        )
        assert status == 200, answer
        result = answer["result"]
        assert isinstance(result, dict)
        return result

    def observe(self, project: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.call(project, tool, {"project_id": project, **arguments})
        assert result["isError"] is False, result
        observed = result["structuredContent"]
        assert isinstance(observed, dict)
        assert observed["project_context_id"] == self.sessions[project]
        return observed

    def wait(self, project: str, operation: str, statuses: set[str]) -> dict[str, Any]:
        deadline = time.monotonic() + 300
        while True:
            observed = self.observe(project, "get_task", {"operation_id": operation})
            if observed["status"] in statuses:
                return observed
            assert time.monotonic() < deadline, observed
            time.sleep(3)


@pytest.fixture(scope="module")
def researcher(cluster: Cluster, probe_image: str) -> Iterator[Researcher]:
    with probe(cluster, "hw-probe-jobs", probe_image) as pod:
        yield Researcher(cluster, pod)


def _count_task(operation: str, **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "operation_id": operation,
        "name": "count rows",
        "image": TASK_IMAGE,
        "command": ["sh", "-c", "wc -l < /inputs/cohort.csv > /outputs/count.txt"],
        "resources": {"cpu_cores": 1, "ram_gb": 0.25, "disk_gb": 1, "max_runtime_seconds": 300},
        "inputs": [{"object_id": "jobs-fixture.cohort.csv", "path": "/inputs/cohort.csv"}],
        "outputs": [{"name": "count.txt", "path": "/outputs/count.txt"}],
        **changes,
    }


@pytest.fixture(scope="module")
def fixture_object(cluster: Cluster) -> None:
    with store(cluster) as objects:
        objects.put(_BUCKET, "datasets/jobs-fixture/cohort.csv", b"id\n1\n2\n3\n")


@pytest.mark.usefixtures("fixture_object")
def test_a_researcher_runs_a_batch_job_through_the_job_service(
    cluster: Cluster, researcher: Researcher
) -> None:
    declared = researcher.call("demo", "describe_operations", {"project_id": "demo"})
    assert declared["isError"] is False, declared
    operations = {
        item["tool"]: item["operation"] for item in declared["structuredContent"]["operations"]
    }
    assert operations == {
        "create_task": "submit",
        "get_task": "status",
        "cancel_task": "cancel",
        "propose_task": "proposal",
    }
    operation = str(uuid.uuid4())
    arguments = {
        key: value for key, value in _count_task(operation).items() if key != "operation_id"
    }
    proposal = researcher.call(
        "demo",
        "propose_task",
        {"project_id": "demo", "tool": "create_task", "arguments": arguments},
    )
    assert proposal["isError"] is False, proposal
    assert "jobs-fixture.cohort.csv" in proposal["structuredContent"]["data_scope"]

    submitted = researcher.observe("demo", "create_task", _count_task(operation))
    assert submitted["status"] in {"queued", "running"}
    # A repeated submission returns the same task and starts no second one.
    again = researcher.observe("demo", "create_task", _count_task(operation))
    assert again["backend_operation_ref"] == submitted["backend_operation_ref"]
    completed = researcher.wait("demo", operation, {"completed", "failed"})
    assert completed["status"] == "completed", completed
    with store(cluster) as objects:
        assert objects.get(_BUCKET, f"results/{operation}/count.txt") == b"4\n"
    # Funnel removes the task's Jobs after completion.
    for _ in range(30):
        result = cluster.kubectl(
            "get",
            "--namespace=hw-jobs-demo",
            "jobs",
            "--output=name",
            f"--selector=heartwood.platform/task={submitted['backend_operation_ref']}",
        )
        assert result.returncode == 0, result.stderr
        if not result.stdout.strip():
            break
        time.sleep(2)
    assert result.stdout.strip() == ""


def test_a_researcher_cancels_a_running_job(researcher: Researcher) -> None:
    operation = str(uuid.uuid4())
    task = _count_task(operation, command=["sleep", "600"], inputs=[], outputs=[])
    researcher.observe("demo", "create_task", task)
    researcher.wait("demo", operation, {"running"})
    cancellation = str(uuid.uuid4())
    observed = researcher.observe(
        "demo", "cancel_task", {"operation_id": cancellation, "target_operation_id": operation}
    )
    assert observed["status"] == "completed"
    assert observed["limitations"][0].startswith(f"Cancellation of {operation} observed")
    assert researcher.wait("demo", operation, {"cancelled", "failed"})["status"] == "cancelled"


def test_the_service_refuses_other_projects_gpus_and_reserved_tags(
    researcher: Researcher,
) -> None:
    operation = str(uuid.uuid4())
    # The demo token cannot act in project other.
    other = researcher.call(
        "demo", "create_task", {"project_id": "other", **_count_task(operation)}
    )
    assert other["isError"] is True
    assert "not the project of the token" in other["content"][0]["text"]
    gpu = researcher.observe(
        "demo",
        "create_task",
        _count_task(operation, resources={"gpu_count": 1}, inputs=[], outputs=[]),
    )
    assert gpu["status"] == "denied"
    tagged = researcher.call(
        "demo",
        "create_task",
        {
            "project_id": "demo",
            **_count_task(str(uuid.uuid4()), tags={"_WORKER_SA": "funnel-server"}),
        },
    )
    assert tagged["isError"] is True
    # Another project's operation does not exist for the caller.
    unknown = researcher.observe("other", "get_task", {"operation_id": operation})
    assert unknown["status"] == "failed"


def test_requests_without_a_job_service_token_are_refused(researcher: Researcher) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for token in (None, "not-a-token"):
        status, _ = request(researcher.cluster, researcher.pod, "POST", f"{JOBS}/mcp", token, body)
        assert status == 401


def test_a_task_created_around_the_job_service_is_cancelled(cluster: Cluster) -> None:
    with executor(cluster, "demo") as funnel:
        tes = funnel.client("jobsvc", job_service_password(cluster, "demo"))
        task_id = tes.create(
            TesTask(
                name="unrecorded",
                resources=TesResources(cpu_cores=1, ram_gb=0.25),
                executors=(TesExecutorSpec(image=TASK_IMAGE, command=("sleep", "600")),),
            )
        )
        # The job service lists the tasks every minute.
        view, states = wait_for(lambda: tes.get(task_id), lambda item: item.state in TERMINAL, 240)
        assert view.state == "CANCELED", states


@pytest.fixture
def paused(cluster: Cluster) -> Iterator[None]:
    """Tasks created around the job service, which would cancel them."""
    with job_service_paused(cluster):
        yield


@pytest.mark.usefixtures("paused")
def test_an_executor_pod_has_no_token_credential_or_network(cluster: Cluster) -> None:
    script = (
        "for target in versitygw.heartwood-platform.svc.cluster.local:7070 "
        "funnel-server.hw-jobs-demo.svc:9090 funnel-server.hw-jobs-demo.svc:8000 "
        "heartwood-jobs.heartwood-platform.svc.cluster.local:8443 "
        "kubernetes.default.svc:443 1.1.1.1:443; "
        'do if nc -z -w 3 "${target%:*}" "${target#*:}" 2>/dev/null; then echo "$target open"; '
        'else echo "$target blocked"; fi; done > /outputs/probe.txt; '
        "ls /var/run/secrets/kubernetes.io/serviceaccount >> /outputs/probe.txt 2>&1 || "
        "echo no-token >> /outputs/probe.txt; "
        "env | grep -c -i -E 'secret|password|token|key' >> /outputs/probe.txt || true"
    )
    run = secrets.token_hex(6)
    with executor(cluster, "demo") as funnel, store(cluster) as objects:
        tes = funnel.client("jobsvc", job_service_password(cluster, "demo"))
        key = f"results/probe-{run}/probe.txt"
        task_id = tes.create(
            TesTask(
                name=f"probe {run}",
                outputs=(
                    TesOutput(url=objects.url("PUT", _BUCKET, key), path="/outputs/probe.txt"),
                ),
                resources=TesResources(cpu_cores=1, ram_gb=0.25, disk_gb=1),
                executors=(TesExecutorSpec(image=TASK_IMAGE, command=("sh", "-c", script)),),
                tags={"heartwood-operation": f"probe-{run}"},
            )
        )
        view, states = wait_for(lambda: tes.get(task_id), lambda item: item.state in TERMINAL)
        assert view.state == "COMPLETE", states
        content = objects.get(_BUCKET, key)
    assert content is not None
    lines = content.decode().splitlines()
    assert [line for line in lines if line.endswith(" open")] == []
    assert len([line for line in lines if line.endswith(" blocked")]) == 6
    assert "no-token" in lines
    assert lines[-1] == "0"


@pytest.mark.usefixtures("paused")
def test_a_worker_credential_reaches_only_its_own_task(cluster: Cluster) -> None:
    with executor(cluster, "demo") as funnel:
        tes = funnel.client("jobsvc", job_service_password(cluster, "demo"))

        def sleeping(name: str) -> str:
            return tes.create(
                TesTask(
                    name=name,
                    resources=TesResources(cpu_cores=1, ram_gb=0.25),
                    executors=(TesExecutorSpec(image=TASK_IMAGE, command=("sleep", "600")),),
                )
            )

        first, second = sleeping("credential a"), sleeping("credential b")
        try:
            wait_for(lambda: tes.get(first), lambda item: item.state == "RUNNING")
            user, password = secret(
                cluster, "hw-jobs-demo", f"funnel-worker-credential-{first}", "credential"
            ).split(":", 1)
            assert user == f"task/{first}"
            with funnel.http(user, password) as worker:
                assert worker.get(f"/v1/tasks/{first}").status_code == 200
                assert worker.get(f"/v1/tasks/{second}").status_code == 403
                assert worker.get("/v1/tasks").status_code == 403
                assert worker.post(f"/v1/tasks/{second}%3Acancel").status_code == 403
                assert worker.post("/v1/tasks", json={"executors": []}).status_code == 403
            assert tes.get(second).state != "CANCELED"
        finally:
            tes.cancel(first)
            tes.cancel(second)


def test_one_projects_job_service_credential_is_refused_by_another_project(
    cluster: Cluster,
) -> None:
    with executor(cluster, "other") as funnel:
        with funnel.http("jobsvc", job_service_password(cluster, "demo")) as client:
            assert client.get("/v1/tasks").status_code == 401
        with funnel.http("jobsvc", job_service_password(cluster, "other")) as client:
            assert client.get("/v1/tasks").status_code == 200


def test_the_executor_api_is_reached_only_by_the_job_service(researcher: Researcher) -> None:
    address = researcher.cluster.kubectl(
        "get",
        "--namespace=hw-jobs-demo",
        "service/funnel-server",
        "--output=jsonpath={.spec.clusterIP}",
    )
    assert address.returncode == 0, address.stderr
    for port in (8000, 9090):
        # The Service address, so that only the network policy can refuse the connection.
        result = researcher.cluster.kubectl(
            "exec",
            f"--namespace={SESSIONS}",
            researcher.pod,
            "--",
            "/agnhost",
            "connect",
            f"{address.stdout.strip()}:{port}",
            "--timeout=5s",
        )
        assert result.returncode != 0
        assert "TIMEOUT" in result.stderr + result.stdout


# The worker Job policy, with a token bound to a pod of the worker ServiceAccount.

_TASK = "0f0e0d0c-0b0a-1009-8807-060504030201"
_OTHER_TASK = "1f1e1d1c-1b1a-1019-8817-161514131211"


@contextmanager
def _worker_tokens(cluster: Cluster, probe_image: str, namespace: str) -> Iterator[tuple[str, str]]:
    """Tokens of the worker of task ``_TASK`` in ``namespace``: bound to its pod, and unbound."""
    pod = f"{_TASK}-abcde"
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod,
                "namespace": namespace,
                "labels": {"heartwood.platform/role": "policy-probe"},
            },
            "spec": {
                "serviceAccountName": "funnel-worker",
                "automountServiceAccountToken": False,
                "hostUsers": False,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": "pause",
                        "image": probe_image,
                        "args": ["pause"],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {"limits": {"cpu": "50m", "memory": "32Mi"}},
                    }
                ],
            },
        }
    )
    try:
        bound = cluster.kubectl(
            "create",
            "token",
            "funnel-worker",
            f"--namespace={namespace}",
            "--bound-object-kind=Pod",
            f"--bound-object-name={pod}",
            "--duration=10m",
        )
        assert bound.returncode == 0, bound.stderr
        unbound = cluster.kubectl(
            "create", "token", "funnel-worker", f"--namespace={namespace}", "--duration=10m"
        )
        assert unbound.returncode == 0, unbound.stderr
        yield bound.stdout.strip(), unbound.stdout.strip()
    finally:
        cluster.kubectl("delete", f"--namespace={namespace}", f"pod/{pod}", "--wait=false")


@pytest.fixture(scope="module")
def worker_token(cluster: Cluster, probe_image: str) -> Iterator[tuple[str, str]]:
    """Tokens of a worker in project demo, which has no GPU entitlement."""
    with _worker_tokens(cluster, probe_image, "hw-jobs-demo") as tokens:
        yield tokens


@pytest.fixture(scope="module")
def gpu_worker_token(cluster: Cluster, probe_image: str) -> Iterator[tuple[str, str]]:
    """Tokens of a worker in project other, which the kind values give the GPU entitlement."""
    with _worker_tokens(cluster, probe_image, "hw-jobs-other") as tokens:
        yield tokens


def _as_worker(cluster: Cluster, token: str, job: dict[str, Any]) -> str:
    """Create a Job in a server-side dry run with the token alone; return the error or ''."""
    view = cluster.kubectl("config", "view", "--minify", "--raw", "--output=json")
    assert view.returncode == 0, view.stderr
    configuration = json.loads(view.stdout)
    server = configuration["clusters"][0]["cluster"]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "kubeconfig"
        path.write_text(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": [{"name": "c", "cluster": server}],
                    "users": [{"name": "worker", "user": {"token": token}}],
                    "contexts": [{"name": "w", "context": {"cluster": "c", "user": "worker"}}],
                    "current-context": "w",
                }
            )
        )
        path.chmod(0o600)
        import subprocess

        result = subprocess.run(
            ["kubectl", "--kubeconfig", str(path), "create", "--dry-run=server", "--filename", "-"],
            input=json.dumps(job),
            capture_output=True,
            text=True,
            timeout=120,
        )
    return "" if result.returncode == 0 else result.stderr


def _executor_job(
    task: str = _TASK,
    namespace: str = "hw-jobs-demo",
    **changes: Any,  # noqa: ANN401
) -> dict[str, Any]:
    labels = {
        "app": "funnel-executor",
        "heartwood.platform/role": "funnel-executor",
        "heartwood.platform/task": task,
    }
    pod: dict[str, Any] = {
        "restartPolicy": "Never",
        "serviceAccountName": "funnel-executor",
        "automountServiceAccountToken": False,
        "hostUsers": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [
            {
                "name": "executor",
                "image": TASK_IMAGE,
                "command": ["true"],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}},
                "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
            }
        ],
        "volumes": [
            {"name": "workspace", "persistentVolumeClaim": {"claimName": f"{task}-workspace"}}
        ],
    }
    pod.update(changes.pop("pod", {}))
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"{task}-0", "namespace": namespace, "labels": labels},
        "spec": {"backoffLimit": 0, "template": {"metadata": {"labels": labels}, "spec": pod}},
    }
    job.update(changes)
    return job


def _restarting_job(container: dict[str, Any]) -> dict[str, Any]:
    job = _executor_job()
    job["spec"]["template"]["spec"]["containers"][0].update(container)
    return job


def _failure_policy_job() -> dict[str, Any]:
    job = _executor_job()
    job["spec"]["podFailurePolicy"] = {
        "rules": [{"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget"}]}]
    }
    return job


def test_a_worker_may_create_an_executor_job_of_its_own_task(
    cluster: Cluster, worker_token: tuple[str, str]
) -> None:
    assert _as_worker(cluster, worker_token[0], _executor_job()) == ""


@pytest.mark.parametrize(
    ("job", "message"),
    [
        # Decision: a worker-created Job may mount only its own task's workspace claim.
        (
            _executor_job(
                pod={
                    "volumes": [
                        {
                            "name": "workspace",
                            "persistentVolumeClaim": {"claimName": f"{_OTHER_TASK}-workspace"},
                        }
                    ]
                }
            ),
            "only their task's workspace claim",
        ),
        (_executor_job(_OTHER_TASK), "task of the worker's pod"),
        (
            _executor_job(
                pod={
                    "volumes": [
                        {"name": "workspace", "secret": {"secretName": "funnel-server-config"}}
                    ]
                }
            ),
            "only their task's workspace claim",
        ),
        (_executor_job(pod={"serviceAccountName": "funnel-server"}), "run as funnel-executor"),
        (_executor_job(pod={"automountServiceAccountToken": True}), "ServiceAccount token"),
        (_executor_job(pod={"hostUsers": True}), "user namespace"),
        (_executor_job(pod={"nodeSelector": {"kubernetes.io/hostname": "x"}}), "scheduling"),
        (_executor_job(pod={"tolerations": [{"operator": "Exists"}]}), "only the GPU taint"),
        (_executor_job(pod={"priorityClassName": "system-node-critical"}), "scheduling"),
        (_executor_job(pod={"schedulingGates": [{"name": "hold"}]}), "scheduling"),
        (
            _executor_job(
                pod={
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "DoNotSchedule",
                        }
                    ]
                }
            ),
            "scheduling",
        ),
        # Each Job runs its one pod once, and the pod runs its container once.
        (_failure_policy_job(), "run one pod once"),
        (_executor_job(pod={"restartPolicy": "OnFailure"}), "run one pod once"),
    ],
)
def test_a_worker_may_not_create_any_other_job(
    cluster: Cluster, worker_token: tuple[str, str], job: dict[str, Any], message: str
) -> None:
    assert message in _as_worker(cluster, worker_token[0], job)


@pytest.mark.parametrize(
    "container",
    [
        {"restartPolicy": "Always"},
        {
            "restartPolicy": "Never",
            "restartPolicyRules": [
                {"action": "Restart", "exitCodes": {"operator": "In", "values": [1]}}
            ],
        },
    ],
)
def test_a_worker_may_not_let_its_executor_container_restart(
    cluster: Cluster, worker_token: tuple[str, str], container: dict[str, Any]
) -> None:
    # The API server keeps a container's own restart policy and rules with the
    # ContainerRestartRules feature, which the kind node version enables by default.
    assert "run one pod once" in _as_worker(cluster, worker_token[0], _restarting_job(container))


_GPU_EXISTS = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
_GPU_EQUAL = {
    "key": "nvidia.com/gpu",
    "operator": "Equal",
    "value": "present",
    "effect": "NoSchedule",
}


def _gpu_job(
    namespace: str, tolerations: list[dict[str, Any]], gpus: str | None = "1"
) -> dict[str, Any]:
    job = _executor_job(namespace=namespace, pod={"tolerations": tolerations})
    (container,) = job["spec"]["template"]["spec"]["containers"]
    if gpus is not None:
        container["resources"]["limits"]["nvidia.com/gpu"] = gpus
    return job


@pytest.mark.parametrize("toleration", [_GPU_EXISTS, _GPU_EQUAL])
def test_a_gpu_task_of_an_entitled_project_tolerates_the_gpu_taint(
    cluster: Cluster, gpu_worker_token: tuple[str, str], toleration: dict[str, Any]
) -> None:
    job = _gpu_job("hw-jobs-other", [toleration])
    assert _as_worker(cluster, gpu_worker_token[0], job) == ""


@pytest.mark.parametrize(
    ("tolerations", "gpus", "message"),
    [
        # A task without GPUs, also with a GPU count of zero.
        ([_GPU_EXISTS], None, "only GPU tasks"),
        ([_GPU_EXISTS], "0", "only GPU tasks"),
        # Any other toleration, or more than the one.
        ([{**_GPU_EXISTS, "key": "node.kubernetes.io/unschedulable"}], "1", "only the GPU taint"),
        ([{**_GPU_EXISTS, "effect": "NoExecute"}], "1", "only the GPU taint"),
        (
            [{**_GPU_EXISTS, "effect": "NoExecute", "tolerationSeconds": 60}],
            "1",
            "only the GPU taint",
        ),
        ([{**_GPU_EQUAL, "value": "absent"}], "1", "only the GPU taint"),
        ([{"key": "nvidia.com/gpu", "operator": "Exists"}], "1", "only the GPU taint"),
        ([_GPU_EXISTS, _GPU_EQUAL], "1", "only the GPU taint"),
    ],
)
def test_an_entitled_project_tolerates_nothing_else(
    cluster: Cluster,
    gpu_worker_token: tuple[str, str],
    tolerations: list[dict[str, Any]],
    gpus: str | None,
    message: str,
) -> None:
    job = _gpu_job("hw-jobs-other", tolerations, gpus)
    assert message in _as_worker(cluster, gpu_worker_token[0], job)


def test_a_project_without_the_gpu_entitlement_does_not_tolerate_the_gpu_taint(
    cluster: Cluster, worker_token: tuple[str, str]
) -> None:
    job = _gpu_job("hw-jobs-demo", [_GPU_EXISTS])
    assert "only GPU tasks" in _as_worker(cluster, worker_token[0], job)


def test_a_worker_may_not_choose_the_selector_of_its_job(
    cluster: Cluster, worker_token: tuple[str, str]
) -> None:
    job = _executor_job()
    labels = job["spec"]["template"]["metadata"]["labels"]
    job["spec"]["manualSelector"] = True
    job["spec"]["selector"] = {"matchLabels": labels}
    assert "choose their selector" in _as_worker(cluster, worker_token[0], job)


def test_a_worker_token_not_bound_to_its_pod_creates_no_job(
    cluster: Cluster, worker_token: tuple[str, str]
) -> None:
    assert "task of the worker's pod" in _as_worker(cluster, worker_token[1], _executor_job())


def test_jobs_namespaces_have_a_quota(cluster: Cluster) -> None:
    for project in ("demo", "other"):
        result = cluster.kubectl("get", f"--namespace=hw-jobs-{project}", "resourcequota/jobs")
        assert result.returncode == 0, result.stderr
