# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Installed application qualification against real workers on the Kubernetes driver.

Cases that do not depend on the driver are the Docker qualification's own case functions,
run here with the three helpers that read a worker and observe absence replaced by their
Kubernetes forms. The other cases cover what only this driver has: the cluster access of
the gateway, pod admission, staging into volume claims and deletion through the cluster.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Any, Never
from uuid import uuid4

import grpc
import pytest
import test_worker_lifecycle_integration as lifecycle
import test_worker_native_integration as native
from worker_kubernetes_fixture import WORKER_PODS, WORKSPACE, KubernetesFixture
from worker_native_fixture import PYTHON

from heartwood.core_adapter import SessionService
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerControlPlane,
)
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.gateway.worker_kubernetes import KubernetesWorkerVolumes, cluster_sha256
from heartwood.session import CommandKind

_STAGING_SHAPE = "ValidatingAdmissionPolicy 'heartwood-staging-shape'"
_POD_SECURITY = 'violates PodSecurity "restricted:latest"'
# The message of the staging policy's rule that each case breaks.
_STAGING_RULES = {
    "pod-other-image": "runs only a staging image",
    "pod-supervisor-label": "carries only the labels heartwood.project",
    "pod-other-user": "runs as user and group 10002",
    "pod-default-account": "uses the ServiceAccount hw-staging without a mounted token",
    "pod-mounted-token": "uses the ServiceAccount hw-staging without a mounted token",
    "pod-secret-volume": "mounts only attempt volume claims",
    "pod-foreign-claim": "mounts only attempt volume claims",
    "claim-other-name": "is named hw-attempt-*, uses the StorageClass heartwood-attempt",
    "claim-other-storage-class": "is named hw-attempt-*, uses the StorageClass heartwood-attempt",
}
_TRANSFER_BYTES = 64 * 1024 * 1024
# The adapter gives each cluster request this long, including each archive transfer.
_REQUEST_SECONDS = 60


def _absent(attempt_id: str) -> None:
    """Require, from outside the adapter, that the cluster shows no object of an attempt."""
    fixture = _current[0]
    assert fixture.objects(attempt_id=attempt_id) == []
    assert fixture.objects(attempt_id=attempt_id.replace("-", "")) == []


def _running_image(fixture: KubernetesFixture, worker: str, sandbox_id: str) -> str:
    """Return the digest the worker's agent container runs, as the manifest names it."""
    pod = json.loads(fixture.outside("get", "pod", f"{WORKSPACE}--{worker}", "--output=json"))
    assert pod["metadata"]["labels"]["openshell.ai/boundary-pair"] == sandbox_id.lower()
    (container,) = [item for item in pod["spec"]["containers"] if item["name"] == "agent"]
    (status,) = [item for item in pod["status"]["containerStatuses"] if item["name"] == "agent"]
    repository, _, digest = container["image"].partition("@")
    assert repository == fixture.worker.partition("@")[0]
    assert status["imageID"] == container["image"]
    return digest


def _read(fixture: KubernetesFixture, worker: str, path: str) -> str:
    observed = fixture.exec(worker, ["/bin/sh", "-c", f"cat {path} 2>/dev/null || true"])
    assert observed.exit_code == 0, observed.stderr
    assert isinstance(observed.stdout, str)
    return observed.stdout


_current: list[KubernetesFixture] = []


@pytest.fixture
def infrastructure(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[KubernetesFixture]:
    output = Path(os.environ["HW_QUALIFICATION_OUTPUT"]) / request.node.name
    output.mkdir(mode=0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    fixture = KubernetesFixture(private, output)
    _current[:] = [fixture]
    # The shared cases reach a worker and observe absence through these four helpers.
    monkeypatch.setattr(native, "_assert_absent", _absent)
    monkeypatch.setattr(lifecycle, "_absent", _absent)
    monkeypatch.setattr(lifecycle, "_read", _read)
    monkeypatch.setattr(lifecycle, "_running_image", _running_image)
    try:
        fixture.start()
        yield fixture
    finally:
        try:
            native._retain_session_diagnostics(
                fixture.applications, output / "session-diagnostics.json"
            )
            # Preserve control credentials and gateway state if revocation cannot settle.
            for application in fixture.applications:
                native._stop(application)
            fixture.revoke_probes()
            remaining = fixture.objects()
        finally:
            # The fixture's class patches end with the case, whatever teardown raised.
            fixture.resources.close()
        assert remaining == [], f"Objects remain in the session namespace: {remaining}"
        shutil.rmtree(private)
        (output / "cleanup.json").write_text('{"complete": true}\n')


def _evidence(fixture: KubernetesFixture, report: dict[str, object]) -> None:
    (fixture.output / "evidence.json").write_text(
        json.dumps(
            {
                **report,
                "driver": "openshell-kubernetes",
                "namespace": fixture.namespace,
                "worker_image": fixture.worker,
                "gateway_image": fixture.gateway_image,
                "sensitive_data_ready": False,
            },
            indent=2,
        )
        + "\n"
    )


def _fenced_without_worker(service: SessionService) -> str:
    """Require a launch that bound no worker and return its attempt."""
    attempt = service.worker_attempt()
    assert attempt is not None
    assert attempt.phase == "fenced"
    assert attempt.runtime is None
    assert service.store.owns_writer
    return str(attempt.intent.attempt_id)


def _settled(service: SessionService, outcome: str) -> None:
    attempt = service.worker_attempt()
    assert attempt is not None
    assert attempt.phase == "revoked"
    assert attempt.revocation is not None
    assert attempt.revocation.outcome == outcome
    assert not service.store.owns_writer
    _absent(str(attempt.intent.attempt_id))


def _pod(fixture: KubernetesFixture, name: str, **changes: Any) -> str:  # noqa: ANN401
    """A pod of the staging shape, with one difference a case asks for."""
    container: dict[str, Any] = {
        "name": "stage",
        "image": fixture.worker,
        "command": ["/bin/sleep", "60"],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
        },
        "resources": {"limits": {"cpu": "1", "memory": "256Mi"}},
    }
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "serviceAccountName": "hw-staging",
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10002,
            "runAsGroup": 10002,
            "fsGroup": 10002,
            "supplementalGroupsPolicy": "Strict",
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
    }
    labels = {"heartwood.project": "case", "heartwood.attempt": "case", "heartwood.role": "staging"}
    labels.update(changes.pop("labels", {}))
    container.update(changes.pop("container", {}))
    spec.update(changes)
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": labels},
            "spec": spec,
        }
    )


def _claim(name: str, storage_class: str) -> str:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": storage_class,
                "resources": {"requests": {"storage": "16Mi"}},
            },
        }
    )


def test_kubernetes_gateway_credential_is_confined(infrastructure: KubernetesFixture) -> None:
    """The adapter's credential is refused everything outside staging, by role or by policy."""
    fixture = infrastructure
    create = ("create", "--filename=-")
    refusals: dict[str, str] = {}

    def refused(
        name: str, expected: str, *arguments: str, stdin: str | None = None, rule: str = ""
    ) -> None:
        code, error = fixture.as_gateway(*arguments, stdin=stdin)
        assert code != 0, f"{name} was permitted"
        assert expected in error, f"{name}: {error}"
        assert rule in error, f"{name}: {error}"
        refusals[name] = f"{expected}: {rule}" if rule else expected

    # The fixed role grants no Secret, no policy change and nothing in another namespace.
    refused("read-secrets", "forbidden", "get", "secrets")
    refused("read-client-certificate", "forbidden", "get", "secret", "openshell-client-tls")
    refused("list-namespaces", "forbidden", "get", "namespaces")
    refused("pods-in-platform-namespace", "forbidden", "get", "pods", "--namespace=kube-system")
    refused(
        "create-network-policy",
        "forbidden",
        *create,
        stdin=json.dumps(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "hw-case"},
                "spec": {"podSelector": {}, "egress": [{}], "policyTypes": ["Egress"]},
            }
        ),
    )
    refused("delete-sandbox", "forbidden", "delete", "sandboxes.agents.x-k8s.io", "hw--case")
    refused("ephemeral-container", "forbidden", "debug", "openshell-0", "--image=" + fixture.worker)
    # The role grants pod and claim creation; the admission policy admits one shape only.
    name = "hw-staging-" + uuid4().hex
    # The shapes the adapter creates are admitted; the requests are dry runs.
    claim = _claim("hw-attempt-" + uuid4().hex[:8], "heartwood-attempt")
    for document in (_pod(fixture, name), claim):
        code, answer = fixture.as_gateway(*create, "--dry-run=server", stdin=document)
        assert code == 0, answer
    # Pod Security answers before the policy for a pod that both refuse.
    refused("pod-host-network", _POD_SECURITY, *create, stdin=_pod(fixture, name, hostNetwork=True))
    for case, document in (
        ("pod-other-image", _pod(fixture, name, container={"image": "docker.io/library/busybox"})),
        (
            "pod-supervisor-label",
            _pod(fixture, name, labels={"openshell.ai/boundary-role": "supervisor"}),
        ),
        (
            "pod-other-user",
            _pod(
                fixture,
                name,
                securityContext={
                    "runAsNonRoot": True,
                    "runAsUser": 10001,
                    "runAsGroup": 10002,
                    "fsGroup": 10002,
                    "supplementalGroupsPolicy": "Strict",
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
            ),
        ),
        ("pod-default-account", _pod(fixture, name, serviceAccountName="default")),
        ("pod-mounted-token", _pod(fixture, name, automountServiceAccountToken=True)),
        (
            "pod-secret-volume",
            _pod(
                fixture,
                name,
                volumes=[{"name": "tls", "secret": {"secretName": "openshell-client-tls"}}],
            ),
        ),
        (
            "pod-foreign-claim",
            _pod(
                fixture,
                name,
                volumes=[
                    {
                        "name": "data",
                        "persistentVolumeClaim": {"claimName": "openshell-data-openshell-0"},
                    }
                ],
            ),
        ),
        ("claim-other-name", _claim("hw-case-" + uuid4().hex[:8], "heartwood-attempt")),
        ("claim-other-storage-class", _claim("hw-attempt-" + uuid4().hex[:8], "standard")),
    ):
        refused(case, _STAGING_SHAPE, *create, stdin=document, rule=_STAGING_RULES[case])
    refused(
        "exec-gateway-pod",
        _STAGING_SHAPE,
        "exec",
        "openshell-0",
        "--",
        "/bin/true",
        rule="opens exec sessions only into pods named hw-staging-*",
    )
    code, answer = fixture.as_gateway("auth", "can-i", "create", "pods")
    assert code == 0, answer
    assert fixture.objects() == []
    _evidence(fixture, {"drill_id": "kubernetes-gateway-credential.v1", "refusals": refusals})


_BOUNDARY_PROBE = """import errno,json,os
from pathlib import Path
assert os.getuid()==10001
assert os.environ['HEARTWOOD_QUALIFICATION_KEY'].startswith('openshell:resolve:env:')
checks={}
projections=['/inputs/summary.json','/inputs/new','/skills/aggregate-export/SKILL.md','/skills/new']
for name in projections:
 try: Path(name).write_text('tampered')
 except OSError as error:
  assert error.errno in (errno.EROFS,errno.EACCES),error
  checks['write '+name]=errno.errorcode[error.errno]
 else: raise AssertionError('projection was writable')
for name in ['/var/run/secrets/kubernetes.io/serviceaccount','/run/secrets/kubernetes.io',
  '/run/docker.sock','/var/run/docker.sock','/run/containerd/containerd.sock','/workspace/.heartwood']:
 assert not Path(name).exists(),name
 checks['absent '+name]=True
assert not [name for name in os.environ if name.startswith('KUBERNETES_')]
checks['kubernetes-service-environment-absent']=True
for name in ['/etc/passwd','/usr/bin/env','/proc/1/environ']:
 try: Path(name).open('ab').close()
 except OSError as error: checks['write '+name]=errno.errorcode[error.errno]
 else: raise AssertionError('system path was writable')
result=json.loads(Path('/workspace/export.json').read_text())
assert result['suppressed'] is True and result['exported'] is False
print(json.dumps({'checks':checks,'output':result}))
"""

_ROUTE_PROBE = """import errno,json,socket,sys
host='kubernetes.default.svc.cluster.local'
qname=b''.join(bytes([len(part)])+part.encode() for part in host.split('.'))+b'\\0'
query=bytes.fromhex('123401000001000000000000')+qname+bytes.fromhex('00010001')
http=b'GET /canary HTTP/1.1\\r\\nHost: canary\\r\\nConnection: close\\r\\n\\r\\n'
results={}
for name,host,port,kind in json.loads(sys.argv[1]):
 transport=socket.SOCK_DGRAM if kind=='udp' else socket.SOCK_STREAM
 try:
  with socket.socket(socket.AF_INET,transport) as connection:
   connection.settimeout(4)
   connection.connect((host,port))
   # A TCP connection that opens is a reachable route, whatever follows.
   if kind=='tcp': results[name]='connected'
   if port==53: payload=query if kind=='udp' else len(query).to_bytes(2,'big')+query
   else: payload=http
   connection.sendall(payload)
   if connection.recv(512): results[name]='reached'
 except socket.timeout: results.setdefault(name,'timeout')
 except OSError as error: results.setdefault(name,errno.errorcode.get(error.errno,str(error.errno)))
print(json.dumps(results))
"""


@pytest.fixture
def canary() -> Iterator[tuple[int, list[bytes]]]:
    """A listener in this pod that records every connection it receives."""
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 19672))
    listener.listen()
    listener.settimeout(0.2)
    received: list[bytes] = []
    stopping = threading.Event()

    def serve() -> None:
        while not stopping.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(2)
                try:
                    received.append(connection.recv(64))
                    connection.sendall(b"ACK")
                except OSError:
                    received.append(b"")

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield 19672, received
    finally:
        stopping.set()
        thread.join(timeout=5)
        listener.close()


def test_kubernetes_workflow_boundary_and_revocation(
    infrastructure: KubernetesFixture, canary: tuple[int, list[bytes]]
) -> None:
    """An approved action runs in an admitted worker that reaches only its model route."""
    import socket

    fixture = infrastructure
    application = native._application(fixture)
    service = application._service("composition")
    try:
        native._submit(
            application,
            CommandKind.CHAT,
            {
                "prompt": (
                    "Use aggregate-export on /inputs/summary.json with a floor of 20 and write "
                    "the suppression decision to /workspace/export.json. All data is synthetic."
                )
            },
        )
        attempt = service.worker_attempt()
        assert attempt is not None
        assert attempt.phase == "active"
        assert attempt.runtime is not None
        attempt_id = str(attempt.intent.attempt_id)
        worker = "hw-" + attempt.intent.attempt_id.hex[:16]
        workload = f"{WORKSPACE}--{worker}"
        supervisor = "os-supervisor-" + attempt.runtime.sandbox_id
        approvals = []
        for tool in ("invoke_skill", "terminal"):
            group = lifecycle._pending(application, tool=tool)
            # The effect of an action exists only after its approval.
            if tool == "terminal":
                assert lifecycle._read(fixture, worker, "/workspace/export.json") == ""
            lifecycle._approve(application, group)
            approvals.append(group)
        lifecycle._settle(application)
        assert len(fixture.provider.observations) == 3
        assert all(row["authorized"] for row in fixture.provider.observations)
        assert [item["exit_code"] for item in lifecycle._executions(application)] == [0, 0]

        # What admission read, as the adapter recorded it.
        (record,) = json.loads((fixture.output / WORKER_PODS).read_text())
        admission = record["admission"]
        assert record["sandbox_id"] == attempt.runtime.sandbox_id
        assert set(record["claims"]) == {"inputs", "skills"}

        # Inside the worker: user, projections, no cluster credential, no runtime socket.
        observation = fixture.exec(worker, [PYTHON, "-I", "-B", "-c", _BOUNDARY_PROBE])
        assert observation.exit_code == 0, observation.stderr
        boundary = json.loads(observation.stdout)

        # Routes: the model route worked through the supervisor; every direct route fails.
        own = socket.gethostbyname(socket.gethostname())
        api = socket.gethostbyname("kubernetes.default.svc")
        dns = Path("/etc/resolv.conf").read_text().split("nameserver", 1)[1].split()[0]
        gateway = socket.gethostbyname(f"openshell.{fixture.namespace}.svc.cluster.local")
        provider = socket.gethostbyname(fixture.provider_host)
        port, received = canary
        targets = [
            ["controller-canary", own, port, "tcp"],
            ["model-route-direct", provider, 19671, "tcp"],
            ["model-route-pod-direct", own, 19671, "tcp"],
            ["kubernetes-api", api, 443, "tcp"],
            ["cluster-dns-tcp", dns, 53, "tcp"],
            ["cluster-dns-udp", dns, 53, "udp"],
            ["sandbox-gateway", gateway, 8080, "tcp"],
            ["metadata-ipv4", "169.254.169.254", 80, "tcp"],
            ["public-address", "192.0.2.1", 443, "tcp"],
        ]
        # The same canary answers a client outside the worker before and after the probe.
        routes = None
        for _ in range(2):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as control:
                control.sendall(b"positive-control")
                assert control.recv(16) == b"ACK"
            if len(received) == 1:
                routes = fixture.exec(
                    worker,
                    [PYTHON, "-I", "-B", "-c", _ROUTE_PROBE, json.dumps(targets)],
                    timeout=90,
                )
                assert routes.exit_code == 0, routes.stderr
        assert routes is not None, received
        observed_routes = json.loads(routes.stdout)
        assert set(observed_routes) == {target[0] for target in targets}
        # A TCP connection that opened, or any answer, means the route was open.
        assert not {"connected", "reached"} & set(observed_routes.values()), observed_routes
        assert received == [b"positive-control", b"positive-control"]
        assert len(fixture.provider.observations) == 3

        # Below the sandbox: a pod that the same NetworkPolicies select as the workload pod,
        # with no sandbox process in it, gets no connection to the same listener either.
        live = json.loads(fixture.outside("get", "pod", workload, "--output=json"))
        probe_labels = {
            key: value
            for key, value in live["metadata"]["labels"].items()
            if key.startswith(("openshell.ai/boundary-", "heartwood."))
        }
        assert probe_labels["openshell.ai/boundary-role"] == "workload", probe_labels
        fixture.outside("get", "networkpolicy", "openshell-sandbox-workloads")
        policy_layer = _policy_probe(fixture, own, port, probe_labels)
        assert policy_layer == "timeout", policy_layer
        assert received == [b"positive-control", b"positive-control"]

        # The gateway's credential opens no session into the worker's pods.
        connections = {}
        for name, arguments in (
            ("exec-workload", ("exec", workload, "--", "/bin/true")),
            ("exec-supervisor", ("exec", supervisor, "--", "/bin/true")),
            ("attach-workload", ("attach", workload)),
            ("port-forward-workload", ("port-forward", "pod/" + workload, ":8000")),
        ):
            code, error = fixture.as_gateway(*arguments)
            assert code != 0, name
            assert _STAGING_SHAPE in error or "forbidden" in error, error
            connections[name] = _STAGING_SHAPE if _STAGING_SHAPE in error else "forbidden"
        assert connections["exec-workload"] == _STAGING_SHAPE

        # Every container of both pods names its image by digest in the cluster's registry,
        # and the runtime reports that it runs the same reference.
        pods = {
            item["metadata"]["name"]: item
            for item in json.loads(fixture.outside("get", "pods", "--output=json"))["items"]
        }
        images = {
            name: {
                container["name"]: {
                    "image": container["image"],
                    "image_id": next(
                        status["imageID"]
                        for group in ("containerStatuses", "initContainerStatuses")
                        for status in pods[name]["status"].get(group, [])
                        if status["name"] == container["name"]
                    ),
                }
                for group in ("containers", "initContainers")
                for container in pods[name]["spec"].get(group, [])
            }
            for name in (workload, supervisor)
        }
        assert images == {
            workload: {
                "agent": {"image": fixture.worker, "image_id": fixture.worker},
                "openshell-sandbox-bootstrap": {
                    "image": fixture.sandbox_image,
                    "image_id": fixture.sandbox_image,
                },
                "workspace-init": {"image": fixture.worker, "image_id": fixture.worker},
            },
            supervisor: {
                "supervisor": {
                    "image": fixture.supervisor_image,
                    "image_id": fixture.supervisor_image,
                }
            },
        }, images
        registry = fixture.worker.partition("/")[0]
        assert fixture.supervisor_image.startswith(registry + "/")
        assert fixture.sandbox_image.startswith(registry + "/")
        namespace = json.loads(fixture.outside("get", "namespace", fixture.namespace, "-o=json"))
        identities = {
            "namespace_uid": namespace["metadata"]["uid"],
            "cluster_sha256": attempt.intent.control_plane.container_runtime_sha256,
            "sandbox_id": attempt.runtime.sandbox_id,
            "sandbox_resource": workload,
            "workload_pod": {"name": workload, "uid": pods[workload]["metadata"]["uid"]},
            "supervisor_pod": {"name": supervisor, "uid": pods[supervisor]["metadata"]["uid"]},
            "claims": record["claims"],
        }
        # The driver's Service and NetworkPolicy of the sandbox, which revocation removes too.
        sandbox_id = attempt.runtime.sandbox_id

        def driver_objects() -> list[str]:
            listed = fixture.outside("get", "services,networkpolicies", "--output=name")
            return sorted(name for name in listed.split() if sandbox_id in name)

        driver_objects_before = driver_objects()
        assert f"service/os-supervisor-{sandbox_id}" in driver_objects_before
        assert f"networkpolicy.networking.k8s.io/os-control-{sandbox_id}" in driver_objects_before
        before = fixture.objects(attempt_id=attempt_id)
        assert f"Pod/{workload}" in before
        assert f"Sandbox/{workload}" in before

        service.audit_log.verify()
        native._stop(application)
        native._assert_revoked(service)
        assert fixture.objects() == []
        # They are owned by the sandbox and removed with it.
        started = time.monotonic()
        while (remaining := driver_objects()) and time.monotonic() - started < 60:
            time.sleep(1)
        assert remaining == []
        driver_objects_seconds = round(time.monotonic() - started, 1)
        native._restore(fixture, application.project, "composition", service.store.audit_path)
        _evidence(
            fixture,
            {
                "drill_id": "kubernetes-workflow.v1",
                "attempt_id": attempt_id,
                "policy_sha256": attempt.runtime.sandbox_policy_sha256,
                "kubernetes_identities": identities,
                "admission": admission,
                "worker_checks": boundary["checks"],
                "output": boundary["output"],
                "route_probes": observed_routes,
                "unsandboxed_workload_pod_probe": policy_layer,
                "route_targets": {
                    name: f"{host}:{port}/{kind}" for name, host, port, kind in targets
                },
                "model_route_requests": len(fixture.provider.observations),
                "outside_positive_controls": len(received),
                "refused_connections": connections,
                "images": images,
                "objects_before_revocation": before,
                "objects_after_revocation": [],
                "driver_objects_before_revocation": driver_objects_before,
                "driver_objects_after_revocation": remaining,
                "driver_objects_absent_after_seconds": driver_objects_seconds,
                "outside_approvals": len(approvals),
                "revoked_before_writer_release": True,
                "protected_state_restore": True,
            },
        )
    finally:
        native._stop(application)


_POLICY_PROBE = """import socket,sys
try:
 socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=6).close()
 print('reached')
except socket.timeout: print('timeout')
except OSError as error: print(type(error).__name__)
"""


def _policy_probe(
    fixture: KubernetesFixture, address: str, port: int, labels: dict[str, str]
) -> str:
    """Connect from a pod that carries a workload pod's policy labels and runs no sandbox."""
    name = "hw-qualification-policy-probe"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 1,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": fixture.worker,
                    "command": [PYTHON, "-I", "-c", _POLICY_PROBE, address, str(port)],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    # A small request, so the probe does not depend on the node's headroom.
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "250m", "memory": "128Mi"},
                    },
                }
            ],
        },
    }
    fixture.outside("create", "--filename=-", stdin=json.dumps(pod))
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            phase = fixture.outside("get", "pod", name, "--output=jsonpath={.status.phase}")
            if phase in {"Succeeded", "Failed"}:
                break
            time.sleep(1)
        # A pod that never ran names the reason in its conditions, such as Unschedulable.
        if phase != "Succeeded":
            try:
                conditions = fixture.outside(
                    "get", "pod", name, "--output=jsonpath={.status.conditions}"
                )
            except AssertionError as error:
                conditions = str(error)
            raise AssertionError((phase, conditions))
        return fixture.outside("logs", name).strip()
    finally:
        fixture.outside("delete", "pod", name, "--wait")


# The harness registers a validating admission webhook for the deletion of a Sandbox
# resource that carries this label, served by this pod on this port; both values are
# _CONFLICT_LABEL and _CONFLICT_PORT of deploy/qualify-kubernetes-worker.py.
_DELETE_CONFLICT_LABEL = "qualification.heartwood/delete-conflict"
_DELETE_CONFLICT_PORT = 19674
_CONFLICTS = 3


class _ConflictingWriter:
    """Change one Sandbox resource while the API server admits each of its first deletions.

    The API server checks the preconditions of a deletion, asks admission, and deletes
    only if the resource is unchanged since it read it. A change made while admission
    waits makes the server read the resource again, find the deletion's resourceVersion
    outdated and answer 409 Conflict, as when another writer lands between the driver's
    lookup and its delete. Only processes on the node reach the listener, because the
    controller's ingress policy admits no pod to its port.
    """

    def __init__(self, fixture: KubernetesFixture, name: str, conflicts: int) -> None:
        self.name, self.namespace = name, fixture.namespace
        self.path = f"/apis/agents.x-k8s.io/v1beta1/namespaces/{fixture.namespace}/sandboxes/{name}"
        self.token = Path("/qualification/fixture/token").read_text().strip()
        self.context = ssl.create_default_context(
            cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        )
        self.remaining = conflicts
        self.reviews: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    request = json.loads(self.rfile.read(length))["request"]
                    with owner.lock:
                        owner._review(request)
                    data = json.dumps(
                        {
                            "apiVersion": "admission.k8s.io/v1",
                            "kind": "AdmissionReview",
                            "response": {"uid": request["uid"], "allowed": True},
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as error:
                    owner.errors.append(f"{type(error).__name__}: {error}")
                    self.send_error(500)

        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(
            "/qualification/provider/server/tls.crt", "/qualification/provider/server/tls.key"
        )
        self.server = ThreadingHTTPServer(("0.0.0.0", _DELETE_CONFLICT_PORT), Handler)
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever)

    def _review(self, request: dict[str, Any]) -> None:
        metadata = request["oldObject"]["metadata"]
        record: dict[str, Any] = {
            "user": request["userInfo"]["username"],
            "operation": request["operation"],
            "name": request["name"],
            "preconditions": (request.get("options") or {}).get("preconditions"),
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
        }
        if (
            request["operation"] == "DELETE"
            and request["resource"]["resource"] == "sandboxes"
            and request["namespace"] == self.namespace
            and request["name"] == self.name
            and not request.get("dryRun")
            and self.remaining > 0
        ):
            self.remaining -= 1
            record["changed_to"] = self._change(len(self.reviews))
        self.reviews.append(record)

    def _change(self, index: int) -> str:
        """Patch the resource and return the resourceVersion the change gave it."""
        connection = http.client.HTTPSConnection(
            "kubernetes.default.svc", context=self.context, timeout=5
        )
        try:
            connection.request(
                "PATCH",
                self.path,
                json.dumps(
                    {"metadata": {"annotations": {"qualification.heartwood/change": str(index)}}}
                ),
                {
                    "Content-Type": "application/merge-patch+json",
                    "Authorization": "Bearer " + self.token,
                },
            )
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"The Sandbox resource change was answered {response.status}")
        return str(json.loads(body)["metadata"]["resourceVersion"])

    def __enter__(self) -> _ConflictingWriter:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_kubernetes_conflicting_deletion_is_retried_until_absence(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion that meets a changed Sandbox resource fails, and revocation sends it again."""
    from openshell import SandboxClient

    fixture = infrastructure
    fixture.provider.script = [("finish", {"message": "Synthetic task completed."})]
    application = native._application(fixture)
    service = application._service("composition")
    answers: list[str] = []
    sent: list[float] = []
    delete = SandboxClient.delete

    def recorded(client: SandboxClient, name: str, **options: Any) -> Any:  # noqa: ANN401
        sent.append(time.monotonic())
        try:
            answer = delete(client, name, **options)
        except grpc.RpcError as error:
            answers.append(f"{error.code().name}: {error.details()}")
            raise
        answers.append(answer.outcome.name)
        sent.append(time.monotonic())
        return answer

    try:
        native._submit(application, CommandKind.CHAT, {"prompt": "Synthetic conflict check."})
        backend = service.backend
        assert isinstance(backend, OpenHandsSdkBackend)
        assert backend.wait_for_idle(120)
        attempt = service.worker_attempt()
        assert attempt is not None
        assert attempt.phase == "active"
        workload = f"{WORKSPACE}--hw-{attempt.intent.attempt_id.hex[:16]}"
        monkeypatch.setattr(SandboxClient, "delete", recorded)
        with _ConflictingWriter(fixture, workload, _CONFLICTS) as writer:
            # Only the deletion of a Sandbox resource with the label reaches the writer.
            fixture.outside(
                "label",
                "sandboxes.agents.x-k8s.io",
                workload,
                f"{_DELETE_CONFLICT_LABEL}=armed",
            )
            started = time.monotonic()
            native._stop(application)
            seconds = round(time.monotonic() - started, 1)
        native._assert_revoked(service)
        assert not writer.errors, writer.errors
        # The driver reports each conflicting delete as an error, revocation sends the
        # deletion again, and a delete that meets no change completes, all within the
        # seven requests and 90 seconds of one revocation. A write by a controller between
        # a lookup and its delete adds a conflict that admission never sees.
        assert answers[-1] == "COMPLETED"
        assert all("retry deletion" in answer for answer in answers[:-1]), answers
        assert _CONFLICTS < len(answers) <= 7, answers
        assert sent[-1] - sent[0] < 90
        deletions = [
            review
            for review in writer.reviews
            if review["operation"] == "DELETE" and review["name"] == workload
        ]
        # The writer changed the resource during the first three deletions it admitted,
        # each of which therefore met a conflict, and left the later ones unchanged.
        assert all("changed_to" in review for review in deletions[:_CONFLICTS]), deletions
        assert all("changed_to" not in review for review in deletions[_CONFLICTS:])
        assert _CONFLICTS < len(deletions) <= len(answers), deletions
        for review in deletions:
            # The OpenShell gateway's account sends each delete, with the resource's UID
            # and resourceVersion as preconditions.
            assert review["user"] == f"system:serviceaccount:{fixture.namespace}:openshell"
            assert review["preconditions"] == {
                "uid": review["uid"],
                "resourceVersion": review["resource_version"],
            }
        # A delete reaches admission only with the current resourceVersion, so each
        # admitted delete after a change followed a new lookup.
        versions = [review["resource_version"] for review in deletions]
        assert all(left != right for left, right in pairwise(versions)), versions
        _evidence(
            fixture,
            {
                "drill_id": "kubernetes-delete-conflict.v1",
                "attempt_id": str(attempt.intent.attempt_id),
                "deletion_answers": answers,
                "conflicting_answers": len(answers) - 1,
                "admission_reviews": deletions,
                "seconds_until_revoked": seconds,
                "objects_after_revocation": fixture.objects(),
                "revoked_before_writer_release": True,
            },
        )
    finally:
        native._stop(application)


def test_kubernetes_unsent_creation_is_confirmed_absent(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creation that never arrived is recorded as not created only on the gateway's word."""
    from openshell import SandboxClient

    fixture = infrastructure
    create = SandboxClient.create
    unsent: dict[str, Any] = {}

    def lose(_client: SandboxClient, **request: Any) -> Never:  # noqa: ANN401
        if "heartwood.attempt" in request.get("labels", {}) and not request["name"].startswith(
            "probe-"
        ):
            unsent.update(request)
            raise TimeoutError("synthetic unsent creation")
        raise AssertionError("Only the attempt's creation is expected here")

    application = native._application(fixture)
    service = application._service("composition")
    monkeypatch.setattr(SandboxClient, "create", lose)
    try:
        native._submit(application, CommandKind.CHAT, {"prompt": "Synthetic unsent creation."})
        attempt_id = _fenced_without_worker(service)
        # Staging had finished: the attempt's claims exist and its staging pod is gone.
        staged = fixture.objects(attempt_id=attempt_id)
        assert len(staged) == 2
        assert all(name.startswith("PersistentVolumeClaim/hw-attempt-") for name in staged)
        assert not fixture.provider.observations
        native._stop(application)
        _settled(service, "not-created")
        # The gateway keeps its answer: the name it reported absent cannot be created.
        monkeypatch.setattr(SandboxClient, "create", create)
        with pytest.raises(grpc.RpcError) as refused:
            fixture.control.create(**unsent)
        assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert "confirmed absent" in refused.value.details()
        assert fixture.objects() == []
        _evidence(
            fixture,
            {
                "drill_id": "kubernetes-unsent-creation.v1",
                "attempt_id": attempt_id,
                "objects_after_failed_launch": staged,
                "revocation_outcome": "not-created",
                "later_creation": refused.value.code().name,
                "objects_after_revocation": [],
                "provider_requests": 0,
            },
        )
    finally:
        native._stop(application)


def _reason(error: grpc.RpcError) -> str | None:
    """The reason of the error details the gateway attaches to a refusal."""
    from google.rpc import error_details_pb2, status_pb2

    for key, value in error.trailing_metadata() or ():  # type: ignore[attr-defined]
        if key == "grpc-status-details-bin":
            status = status_pb2.Status.FromString(value)
            for detail in status.details:
                info = error_details_pb2.ErrorInfo()
                if detail.Unpack(info):
                    return str(info.reason)
    return None


def test_kubernetes_repeated_creation_is_not_executed_after_revocation(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creation delivered again after its sandbox was revoked creates nothing."""
    from openshell import SandboxClient

    fixture = infrastructure
    create = SandboxClient.create
    delivered: dict[str, Any] = {}

    def lose_reply(client: SandboxClient, **request: Any) -> Never:  # noqa: ANN401
        if "heartwood.attempt" not in request.get("labels", {}):
            raise AssertionError("Only the attempt's creation is expected here")
        delivered.update(request)
        create(client, **request)
        raise TimeoutError("synthetic lost creation reply")

    application = native._application(fixture)
    service = application._service("composition")
    monkeypatch.setattr(SandboxClient, "create", lose_reply)
    try:
        native._submit(application, CommandKind.CHAT, {"prompt": "Synthetic repeated creation."})
        attempt_id = _fenced_without_worker(service)
        assert delivered["request_id"] == attempt_id
        native._stop(application)
        _settled(service, "revoked")
        # The same request arrives again: the gateway refuses to execute it a second time.
        monkeypatch.setattr(SandboxClient, "create", create)
        with pytest.raises(grpc.RpcError) as refused:
            fixture.control.create(**delivered)
        assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert _reason(refused.value) == "REQUEST_REPLAY_UNAVAILABLE"
        time.sleep(10)
        assert fixture.objects() == []
        _evidence(
            fixture,
            {
                "drill_id": "kubernetes-repeated-creation.v1",
                "attempt_id": attempt_id,
                "request_id": delivered["request_id"],
                "revocation_outcome": "revoked",
                "repeated_creation": refused.value.code().name,
                "objects_after_repeated_creation": [],
                "provider_requests": len(fixture.provider.observations),
            },
        )
    finally:
        native._stop(application)


def test_kubernetes_staging_name_workspace_creates_no_pod(
    infrastructure: KubernetesFixture,
) -> None:
    """A sandbox in a workspace with a staging name never gets a pod of that name."""
    from openshell import WorkspaceClient
    from openshell._proto import openshell_pb2 as pb

    fixture = infrastructure
    # The gateway's client certificate can create a workspace; its sandboxes would carry
    # the workspace name, which the gateway's credential may open exec sessions into.
    workspace, name = "hw-staging-q", "case"
    workspaces = WorkspaceClient.from_sandbox_client(fixture.control)
    workspaces.create(workspace)
    labels = {"heartwood.project": fixture.identity, "heartwood.attempt": name}
    refusal: str | None = None
    refusal_details = ""
    try:
        try:
            fixture.control.create(
                workspace=workspace,
                name=name,
                labels=labels,
                spec=pb.SandboxSpec(
                    template=pb.SandboxTemplate(
                        image=fixture.worker,
                        resources={"limits": {"cpu": "1", "memory": "2Gi"}},
                        labels={**labels, "heartwood.role": "worker"},
                    ),
                    policy=fixture.policy,
                    command=[PYTHON, "-I", "-c", "import time; time.sleep(600)"],
                ),
            )
        except grpc.RpcError as error:
            refusal, refusal_details = error.code().name, str(error.details())
        staged: list[str] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            listed = json.loads(
                fixture.outside("get", "sandboxes.agents.x-k8s.io,pods", "--output=json")
            )
            staged = [
                f"{item['kind']}/{item['metadata']['name']}"
                for item in listed["items"]
                if item["metadata"]["name"].startswith(workspace)
            ]
            assert staged == []
            time.sleep(2)
    finally:
        with contextlib.suppress(grpc.RpcError):
            fixture.control.delete(
                name, workspace=workspace, allow_missing=True, timeout_seconds=90
            )
        with contextlib.suppress(grpc.RpcError):
            workspaces.delete(workspace, allow_missing=True)
    # The driver creates the Sandbox resource during the creation request, so the admission
    # policy's refusal is the request's error.
    assert refusal is not None
    assert "heartwood-staging-names" in refusal_details, refusal_details
    _evidence(
        fixture,
        {
            "drill_id": "kubernetes-staging-name.v1",
            "workspace": workspace,
            "creation_refusal": refusal,
            "creation_refusal_details": refusal_details,
            "objects_of_the_workspace": staged,
        },
    )


def _refused_launch(
    fixture: KubernetesFixture, method: str, **deployment: str
) -> tuple[SessionService, str, list[str]]:
    """Run one launch that the adapter must refuse, and return why it did."""
    refusals: list[str] = []
    original = getattr(KubernetesWorkerVolumes, method)

    def observed(volumes: KubernetesWorkerVolumes, *arguments: Any) -> Any:  # noqa: ANN401
        try:
            return original(volumes, *arguments)
        except WorkerAttemptError as error:
            refusals.append(str(error))
            raise

    # The observer is removed before the case ends, so the fixture's own recorder, which
    # it wraps, is restored in the order it was installed.
    setattr(KubernetesWorkerVolumes, method, observed)
    try:
        settings = fixture.deployment(WorkerInputManifest(files=()), **deployment)
        application = native._application(fixture, deployment=settings)
        service = application._service("composition")
        native._submit(application, CommandKind.CHAT, {"prompt": "Synthetic refused launch."})
        attempt_id = _fenced_without_worker(service)
        assert not fixture.provider.observations
        native._stop(application)
    finally:
        setattr(KubernetesWorkerVolumes, method, original)
    return service, attempt_id, refusals


def test_kubernetes_admission_refuses_another_supervisor_image(
    infrastructure: KubernetesFixture,
) -> None:
    fixture = infrastructure
    service, attempt_id, refusals = _refused_launch(
        fixture, "inspect_worker", supervisor_image_digest="sha256:" + "0" * 64
    )
    assert refusals == ["native worker image does not match admission"]
    _settled(service, "revoked")
    (record,) = json.loads((fixture.output / WORKER_PODS).read_text())
    _evidence(
        fixture,
        {
            "drill_id": "kubernetes-admission-image-digest.v1",
            "attempt_id": attempt_id,
            "refusal": refusals[0],
            "expected_supervisor_digest": "sha256:" + "0" * 64,
            "admission": record["admission"],
            "objects_after_revocation": fixture.objects(),
            "provider_requests": 0,
        },
    )


def test_kubernetes_admission_refuses_an_added_network_policy(
    infrastructure: KubernetesFixture,
) -> None:
    fixture = infrastructure
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "hw-qualification-added-egress"},
        "spec": {
            "podSelector": {"matchLabels": {"openshell.ai/boundary-role": "workload"}},
            "policyTypes": ["Egress"],
            "egress": [{}],
        },
    }
    fixture.outside("create", "--filename=-", stdin=json.dumps(policy))
    try:
        service, attempt_id, refusals = _refused_launch(fixture, "inspect_worker")
    finally:
        fixture.outside("delete", "networkpolicy", "hw-qualification-added-egress")
    assert refusals == ["native worker network policies do not match admission"]
    _settled(service, "revoked")
    (record,) = json.loads((fixture.output / WORKER_PODS).read_text())
    assert "hw-qualification-added-egress" in json.dumps(record["admission"])
    _evidence(
        fixture,
        {
            "drill_id": "kubernetes-admission-added-policy.v1",
            "attempt_id": attempt_id,
            "refusal": refusals[0],
            "added_policy": policy,
            "admission": record["admission"],
            "objects_after_revocation": fixture.objects(),
            "provider_requests": 0,
        },
    )


def test_kubernetes_staging_requires_the_default_deny_policy(
    infrastructure: KubernetesFixture,
) -> None:
    fixture = infrastructure
    saved = json.loads(fixture.outside("get", "networkpolicy", "hw-default-deny", "-o=json"))
    restored = {
        "apiVersion": saved["apiVersion"],
        "kind": saved["kind"],
        "metadata": {"name": "hw-default-deny"},
        "spec": saved["spec"],
    }
    fixture.outside("delete", "networkpolicy", "hw-default-deny")
    try:
        service, attempt_id, refusals = _refused_launch(fixture, "stage")
        created = fixture.objects()
    finally:
        fixture.outside("create", "--filename=-", stdin=json.dumps(restored))
    assert refusals == ["worker staging requires a pod without network access"]
    assert created == []
    _settled(service, "not-created")
    _evidence(
        fixture,
        {
            "drill_id": "kubernetes-staging-default-deny.v1",
            "attempt_id": attempt_id,
            "refusal": refusals[0],
            "objects_created": created,
            "revocation_outcome": "not-created",
            "provider_requests": 0,
        },
    )


def test_kubernetes_staging_transfers_a_large_input_within_the_request_limit(
    infrastructure: KubernetesFixture,
) -> None:
    """One 64 MiB input reaches its claim inside the time one cluster request may take."""
    fixture = infrastructure
    settings = fixture.deployment(WorkerInputManifest(files=()))
    sources = {}
    for mount in ("inputs", "skills"):
        sources[mount] = fixture.private / ("large-" + mount)
        sources[mount].mkdir(mode=0o700)
    with (sources["inputs"] / "synthetic.bin").open("wb") as stream:
        for _ in range(_TRANSFER_BYTES // (1024 * 1024)):
            stream.write(os.urandom(1024 * 1024))
    (sources["skills"] / "SKILL.md").write_text("# Synthetic\n")
    intent = WorkerAttemptIntent(
        project_id=uuid4(),
        session_id="transfer",
        attempt_id=uuid4(),
        conversation_id=uuid4(),
        image_digest=settings.image_digest,
        gateway_policy_sha256="c" * 64,
        sandbox_policy_sha256=settings.sandbox_policy_sha256,
        input_manifest_sha256="d" * 64,
        control_plane=WorkerControlPlane(
            control_plane_sha256="e" * 64, container_runtime_sha256=cluster_sha256(settings)
        ),
    )
    volumes = KubernetesWorkerVolumes(settings, intent)
    started = time.monotonic()
    try:
        volumes.stage(sources)
        seconds = round(time.monotonic() - started, 1)
        claims = dict(volumes.claims)
        staged = fixture.objects(attempt_id=str(intent.attempt_id))
        assert len(staged) == 2
    finally:
        volumes.remove_after_revocation()
    assert fixture.objects() == []
    # The whole staging, with the image pull and both transfers, stayed below what a
    # single transfer may take.
    assert seconds < _REQUEST_SECONDS
    _evidence(
        fixture,
        {
            "drill_id": "kubernetes-staging-transfer.v1",
            "transferred_bytes": _TRANSFER_BYTES,
            "staging_seconds": seconds,
            "request_limit_seconds": _REQUEST_SECONDS,
            "claims": claims,
            "objects_after_removal": [],
        },
    )


def test_kubernetes_lost_creation_reply_is_revoked_during_provisioning(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    native.test_lost_create_retains_writer_until_native_revocation(infrastructure, monkeypatch)


def test_kubernetes_revoked_worker_is_replaced_for_the_next_command(
    infrastructure: KubernetesFixture,
) -> None:
    native.test_native_revoked_worker_is_replaced_for_the_next_command(infrastructure)


@pytest.mark.parametrize("pending", [False, True], ids=["settled", "pending"])
def test_kubernetes_gateway_restart_reconciles_the_session(
    infrastructure: KubernetesFixture, pending: bool
) -> None:
    lifecycle.test_native_gateway_restart_reconciles_the_session(infrastructure, pending)


def test_kubernetes_cancellation_settles_without_repeating_the_effect(
    infrastructure: KubernetesFixture,
) -> None:
    lifecycle.test_native_cancellation_settles_without_repeating_the_effect(infrastructure)


# Fills memory past the container's limit, so the kernel ends the workload container.
_EXHAUST_MEMORY = "chunks = []\nwhile True:\n    chunks.append(b'x' * (64 * 1024 * 1024))\n"


def test_kubernetes_worker_container_is_never_restarted(
    infrastructure: KubernetesFixture,
) -> None:
    """A workload container that ends through its memory limit is never restarted."""
    from openshell._proto import openshell_pb2 as pb

    fixture = infrastructure
    name, sandbox_id = fixture.create_probe(fixture.worker)
    pod = f"{WORKSPACE}--{name}"
    try:
        ready = fixture.control.wait_ready(name, workspace=WORKSPACE, timeout_seconds=240)
        assert ready.id == sandbox_id
        spec = json.loads(fixture.outside("get", "pod", pod, "--output=json"))["spec"]
        assert spec["restartPolicy"] == "Never"
        with contextlib.suppress(grpc.RpcError):
            fixture.exec(name, [PYTHON, "-I", "-c", _EXHAUST_MEMORY], timeout=120)
        agent: dict[str, Any] = {}
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = json.loads(fixture.outside("get", "pod", pod, "--output=json"))["status"]
            (agent,) = [item for item in status["containerStatuses"] if item["name"] == "agent"]
            if "terminated" in agent["state"]:
                break
            time.sleep(2)
        assert "terminated" in agent["state"], agent
        # The container stays ended, and the gateway no longer reports the sandbox ready.
        time.sleep(30)
        status = json.loads(fixture.outside("get", "pod", pod, "--output=json"))["status"]
        (agent,) = [item for item in status["containerStatuses"] if item["name"] == "agent"]
        assert agent["restartCount"] == 0, agent
        assert agent["state"].get("terminated", {}).get("reason") == "OOMKilled", agent
        phase = fixture.control.get(name, workspace=WORKSPACE).phase
        assert phase != pb.SANDBOX_PHASE_READY, phase
        _evidence(
            fixture,
            {
                "drill_id": "kubernetes-worker-container-lifetime.v1",
                "sandbox_id": sandbox_id,
                "restart_policy": spec["restartPolicy"],
                "agent_termination": agent["state"]["terminated"].get("reason"),
                "agent_restarts": agent["restartCount"],
                "sandbox_phase_after": phase,
            },
        )
    finally:
        fixture.revoke_probes()


_SYMLINK_PROBE = """import json,os
from pathlib import Path
workspace,link=Path('/workspace'),Path('/workspace-link')
print(json.dumps({'workspace_is_directory':workspace.is_dir() and not workspace.is_symlink(),
 'workspace_is_mount':os.path.ismount(workspace),
 'link_resolves_to_workspace':link.is_symlink() and link.resolve()==workspace,
 'sandbox_is_mount':os.path.ismount('/sandbox')}))
"""


def test_kubernetes_symlink_workspace_image_runs_without_its_working_directory(
    infrastructure: KubernetesFixture,
) -> None:
    """The driver gives every worker the same workspace mount, whatever the image names."""
    fixture = infrastructure
    # The same provider, policy and limits admit the parent image.
    accepted_policy = fixture.qualify_policy()
    name, sandbox_id = fixture.create_probe(fixture.symlink_image)
    try:
        ready = fixture.control.wait_ready(name, workspace=WORKSPACE, timeout_seconds=240)
        assert ready.id == sandbox_id
        # The driver does not read the image's working directory: it mounts its workspace
        # claim at /sandbox and registers /sandbox as the main process's working directory,
        # so the link selects nothing.
        pod = json.loads(fixture.outside("get", "pod", f"{WORKSPACE}--{name}", "--output=json"))
        (agent,) = [item for item in pod["spec"]["containers"] if item["name"] == "agent"]
        mounts = {mount["mountPath"]: mount["name"] for mount in agent["volumeMounts"]}
        (claim,) = [
            volume["persistentVolumeClaim"]["claimName"]
            for volume in pod["spec"]["volumes"]
            if volume["name"] == "workspace"
        ]
        assert mounts["/sandbox"] == "workspace"
        assert claim == f"workspace-{WORKSPACE}--{name}"
        assert not {"/workspace", "/workspace-link"} & set(mounts)
        assert "workingDir" not in agent
        observation = fixture.exec(name, [PYTHON, "-I", "-B", "-c", _SYMLINK_PROBE])
        assert observation.exit_code == 0, observation.stderr
        checks = json.loads(observation.stdout)
        # /workspace is the image's own directory, where the worker launcher changes to.
        assert checks == {
            "workspace_is_directory": True,
            "workspace_is_mount": False,
            "link_resolves_to_workspace": True,
            "sandbox_is_mount": True,
        }, checks
        assert not fixture.provider.observations
        _evidence(
            fixture,
            {
                "drill_id": "kubernetes-workspace-symlink.v1",
                "symlink_image": fixture.symlink_image,
                "sandbox_id": sandbox_id,
                "positive_control_policy_sha256": accepted_policy,
                "agent_mounts": mounts,
                "agent_working_directory": agent.get("workingDir"),
                "workspace_claim": {"claim": claim, "mount_path": "/sandbox"},
                "worker_checks": checks,
                "provider_requests": 0,
            },
        )
    finally:
        fixture.revoke_probes()


def test_kubernetes_new_worker_image_and_rotated_credential_after_restart(
    infrastructure: KubernetesFixture,
) -> None:
    try:
        lifecycle.test_native_new_worker_image_and_rotated_credential_after_restart(infrastructure)
    finally:
        infrastructure.keep_credential()


def test_kubernetes_migrated_project_serves_through_the_operator_manifest(
    infrastructure: KubernetesFixture,
) -> None:
    lifecycle.test_native_migrated_project_serves_through_the_operator_manifest(infrastructure)
