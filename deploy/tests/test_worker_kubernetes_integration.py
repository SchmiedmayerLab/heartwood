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

import http.client
import json
import os
import shutil
import ssl
import threading
import time
from collections.abc import Iterator
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
_TRANSFER_BYTES = 64 * 1024 * 1024
# The adapter gives each cluster request this long, including each archive transfer.
_REQUEST_SECONDS = 60


def _absent(attempt_id: str) -> None:
    """Require, from outside the adapter, that the cluster shows no object of an attempt."""
    fixture = _current[0]
    assert fixture.objects(attempt_id=attempt_id) == []
    assert fixture.objects(attempt_id=attempt_id.replace("-", "")) == []


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
    # The shared cases reach a worker and observe absence through these three helpers.
    monkeypatch.setattr(native, "_assert_absent", _absent)
    monkeypatch.setattr(lifecycle, "_absent", _absent)
    monkeypatch.setattr(lifecycle, "_read", _read)
    try:
        fixture.start()
        yield fixture
    finally:
        native._retain_session_diagnostics(
            fixture.applications, output / "session-diagnostics.json"
        )
        # Preserve control credentials and gateway state if revocation cannot settle.
        for application in fixture.applications:
            native._stop(application)
        fixture.revoke_probes()
        remaining = fixture.objects()
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


def _pod(fixture: KubernetesFixture, name: str, **changes: Any) -> str:
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
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
    }
    container.update(changes.pop("container", {}))
    spec.update(changes)
    return json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": spec})


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

    def refused(name: str, expected: str, *arguments: str, stdin: str | None = None) -> None:
        code, error = fixture.as_gateway(*arguments, stdin=stdin)
        assert code != 0, f"{name} was permitted"
        assert expected in error, f"{name}: {error}"
        refusals[name] = expected

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
    for case, document in (
        ("pod-other-image", _pod(fixture, name, container={"image": "docker.io/library/busybox"})),
        ("pod-default-account", _pod(fixture, name, serviceAccountName="default")),
        ("pod-mounted-token", _pod(fixture, name, automountServiceAccountToken=True)),
        ("pod-host-network", _pod(fixture, name, hostNetwork=True)),
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
        refused(case, _STAGING_SHAPE, *create, stdin=document)
    refused("exec-gateway-pod", _STAGING_SHAPE, "exec", "openshell-0", "--", "/bin/true")
    code, answer = fixture.as_gateway("auth", "can-i", "create", "pods")
    assert code == 0, answer
    assert fixture.objects() == []
    _evidence(fixture, {"check_id": "kubernetes-gateway-credential.v1", "refusals": refusals})


_BOUNDARY_PROBE = """import errno,json,os
from pathlib import Path
assert os.getuid()==10001
assert os.environ['HEARTWOOD_QUALIFICATION_KEY'].startswith('openshell:resolve:env:')
checks={}
for name in ['/inputs/summary.json','/inputs/new','/skills/aggregate-export/SKILL.md','/skills/new']:
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
results={}
for name,host,port,kind in json.loads(sys.argv[1]):
 transport=socket.SOCK_DGRAM if kind=='udp' else socket.SOCK_STREAM
 try:
  with socket.socket(socket.AF_INET,transport) as connection:
   connection.settimeout(4)
   connection.connect((host,port))
   connection.sendall(b'GET /canary HTTP/1.1\\r\\nHost: canary\\r\\nConnection: close\\r\\n\\r\\n')
   data=connection.recv(512)
   results[name]='reached' if data else 'closed'
 except socket.timeout: results[name]='timeout'
 except OSError as error: results[name]=errno.errorcode.get(error.errno,str(error.errno))
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
        assert "refusal" not in record

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
        observed_routes = json.loads(routes.stdout)
        assert set(observed_routes) == {target[0] for target in targets}
        assert all(outcome != "reached" for outcome in observed_routes.values()), observed_routes
        assert received == [b"positive-control", b"positive-control"]
        assert len(fixture.provider.observations) == 3

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

        # Both images were pulled by digest, the worker image from the cluster's registry.
        pods = {
            item["metadata"]["name"]: item
            for item in json.loads(fixture.outside("get", "pods", "--output=json"))["items"]
        }
        images = {
            name: {
                status["name"]: {"image": status["image"], "image_id": status["imageID"]}
                for status in pods[name]["status"]["containerStatuses"]
            }
            for name in (workload, supervisor)
        }
        worker_digest = fixture.worker.partition("@")[2]
        assert images[workload]["agent"]["image"] == fixture.worker
        assert images[workload]["agent"]["image_id"].endswith(worker_digest)
        supervisor_digest = fixture.images["supervisor"].partition("@")[2]
        assert all(
            status["image"].endswith(supervisor_digest)
            and status["image_id"].endswith(supervisor_digest)
            for status in images[supervisor].values()
        )
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
        before = fixture.objects(attempt_id=attempt_id)
        assert f"Pod/{workload}" in before
        assert f"Sandbox/{workload}" in before

        service.audit_log.verify()
        native._stop(application)
        native._assert_revoked(service)
        assert fixture.objects() == []
        native._restore(fixture, application.project, "composition", service.store.audit_path)
        _evidence(
            fixture,
            {
                "check_id": "kubernetes-workflow.v1",
                "attempt_id": attempt_id,
                "policy_sha256": attempt.runtime.sandbox_policy_sha256,
                "kubernetes_identities": identities,
                "admission": admission,
                "worker_checks": boundary["checks"],
                "output": boundary["output"],
                "route_probes": observed_routes,
                "route_targets": {
                    name: f"{host}:{port}/{kind}" for name, host, port, kind in targets
                },
                "model_route_requests": len(fixture.provider.observations),
                "outside_positive_controls": len(received),
                "refused_connections": connections,
                "images": images,
                "objects_before_revocation": before,
                "objects_after_revocation": [],
                "outside_approvals": len(approvals),
                "revoked_before_writer_release": True,
                "protected_state_restore": True,
            },
        )
    finally:
        native._stop(application)


class _Writers:
    """Change the Sandbox resource many times a second, as a controller in conflict would."""

    def __init__(self, fixture: KubernetesFixture, name: str) -> None:
        self.path = f"/apis/agents.x-k8s.io/v1beta1/namespaces/{fixture.namespace}/sandboxes/{name}"
        self.token = Path("/qualification/fixture/token").read_text().strip()
        self.context = ssl.create_default_context(
            cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        )
        self.stop = threading.Event()
        self.patches = 0
        self.threads = [threading.Thread(target=self._touch, args=(index,)) for index in range(6)]

    def _touch(self, index: int) -> None:
        count = 0
        while not self.stop.is_set():
            count += 1
            body = json.dumps(
                {
                    "metadata": {
                        "annotations": {f"qualification.heartwood/touch-{index}": str(count)}
                    }
                }
            )
            try:
                connection = http.client.HTTPSConnection(
                    "kubernetes.default.svc", context=self.context, timeout=5
                )
                connection.request(
                    "PATCH",
                    self.path,
                    body,
                    {
                        "Content-Type": "application/merge-patch+json",
                        "Authorization": "Bearer " + self.token,
                    },
                )
                response = connection.getresponse()
                response.read()
                connection.close()
            except OSError:
                continue
            if response.status == 200:
                self.patches += 1
            elif response.status == 404:
                return

    def __enter__(self) -> _Writers:
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=10)


def test_kubernetes_conflicting_deletion_is_retried_until_absence(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion that collides with another writer fails, and revocation sends it again."""
    from openshell import SandboxClient

    fixture = infrastructure
    fixture.provider.script = [("finish", {"message": "Synthetic task completed."})]
    application = native._application(fixture)
    service = application._service("composition")
    answers: list[str] = []
    delete = SandboxClient.delete

    def recorded(client: SandboxClient, name: str, **options: Any) -> Any:
        try:
            answer = delete(client, name, **options)
        except grpc.RpcError as error:
            answers.append(f"{error.code().name}: {error.details()}")
            raise
        answers.append(answer.outcome.name)
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
        started = time.monotonic()
        with _Writers(fixture, workload) as writers:
            time.sleep(1)
            assert writers.patches > 0
            threading.Timer(4, writers.stop.set).start()
            native._stop(application)
        seconds = round(time.monotonic() - started, 1)
        native._assert_revoked(service)
        conflicts = [answer for answer in answers if "retry deletion" in answer]
        assert conflicts, answers
        assert answers[-1] == "COMPLETED"
        _evidence(
            fixture,
            {
                "check_id": "kubernetes-delete-conflict.v1",
                "attempt_id": str(attempt.intent.attempt_id),
                "deletion_answers": answers,
                "conflicting_answers": len(conflicts),
                "sandbox_patches": writers.patches,
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

    def lose(client: SandboxClient, **request: Any) -> Never:
        if "heartwood.attempt" in request.get("labels", {}) and not request["name"].startswith(
            "probe-"
        ):
            unsent.update(request)
            raise TimeoutError("synthetic unsent creation")
        return create(client, **request)  # type: ignore[return-value]

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
                "check_id": "kubernetes-unsent-creation.v1",
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


def _refused_launch(
    fixture: KubernetesFixture, monkeypatch: pytest.MonkeyPatch, method: str, **deployment: Any
) -> tuple[SessionService, str, list[str]]:
    """Run one launch that the adapter must refuse, and return why it did."""
    refusals: list[str] = []
    original = getattr(KubernetesWorkerVolumes, method)

    def observed(volumes: KubernetesWorkerVolumes, *arguments: Any) -> Any:
        try:
            return original(volumes, *arguments)
        except WorkerAttemptError as error:
            refusals.append(str(error))
            raise

    monkeypatch.setattr(KubernetesWorkerVolumes, method, observed)
    settings = fixture.deployment(WorkerInputManifest(files=()), **deployment)
    application = native._application(fixture, deployment=settings)
    service = application._service("composition")
    native._submit(application, CommandKind.CHAT, {"prompt": "Synthetic refused launch."})
    attempt_id = _fenced_without_worker(service)
    assert not fixture.provider.observations
    native._stop(application)
    return service, attempt_id, refusals


def test_kubernetes_admission_refuses_another_supervisor_image(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = infrastructure
    service, attempt_id, refusals = _refused_launch(
        fixture, monkeypatch, "inspect_worker", supervisor_image_digest="sha256:" + "0" * 64
    )
    assert refusals == ["native worker supervisor pod does not match admission"]
    _settled(service, "revoked")
    (record,) = json.loads((fixture.output / WORKER_PODS).read_text())
    _evidence(
        fixture,
        {
            "check_id": "kubernetes-admission-image-digest.v1",
            "attempt_id": attempt_id,
            "refusal": refusals[0],
            "expected_supervisor_digest": "sha256:" + "0" * 64,
            "admission": record["admission"],
            "objects_after_revocation": fixture.objects(),
            "provider_requests": 0,
        },
    )


def test_kubernetes_admission_refuses_an_added_network_policy(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
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
        service, attempt_id, refusals = _refused_launch(fixture, monkeypatch, "inspect_worker")
    finally:
        fixture.outside("delete", "networkpolicy", "hw-qualification-added-egress")
    assert refusals == ["native worker network policies do not match admission"]
    _settled(service, "revoked")
    (record,) = json.loads((fixture.output / WORKER_PODS).read_text())
    assert "hw-qualification-added-egress" in json.dumps(record["admission"])
    _evidence(
        fixture,
        {
            "check_id": "kubernetes-admission-added-policy.v1",
            "attempt_id": attempt_id,
            "refusal": refusals[0],
            "added_policy": policy,
            "admission": record["admission"],
            "objects_after_revocation": fixture.objects(),
            "provider_requests": 0,
        },
    )


def test_kubernetes_staging_requires_the_default_deny_policy(
    infrastructure: KubernetesFixture, monkeypatch: pytest.MonkeyPatch
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
        service, attempt_id, refusals = _refused_launch(fixture, monkeypatch, "stage")
        created = fixture.objects()
    finally:
        fixture.outside("create", "--filename=-", stdin=json.dumps(restored))
    assert refusals == ["worker staging requires a pod without network access"]
    assert created == []
    _settled(service, "not-created")
    _evidence(
        fixture,
        {
            "check_id": "kubernetes-staging-default-deny.v1",
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
            gateway_registration_sha256="e" * 64,
            gateway_endpoint_sha256="f" * 64,
            container_runtime_sha256=cluster_sha256(settings),
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
            "check_id": "kubernetes-staging-transfer.v1",
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
    native.test_lost_create_retains_writer_until_native_revocation(infrastructure, monkeypatch)  # type: ignore[arg-type]


def test_kubernetes_revoked_worker_is_replaced_for_the_next_command(
    infrastructure: KubernetesFixture,
) -> None:
    native.test_native_revoked_worker_is_replaced_for_the_next_command(infrastructure)  # type: ignore[arg-type]


@pytest.mark.parametrize("pending", [False, True], ids=["settled", "pending"])
def test_kubernetes_gateway_restart_reconciles_the_session(
    infrastructure: KubernetesFixture, pending: bool
) -> None:
    lifecycle.test_native_gateway_restart_reconciles_the_session(infrastructure, pending)  # type: ignore[arg-type]


def test_kubernetes_cancellation_settles_without_repeating_the_effect(
    infrastructure: KubernetesFixture,
) -> None:
    lifecycle.test_native_cancellation_settles_without_repeating_the_effect(infrastructure)  # type: ignore[arg-type]


def test_kubernetes_migrated_project_serves_through_the_operator_manifest(
    infrastructure: KubernetesFixture,
) -> None:
    lifecycle.test_native_migrated_project_serves_through_the_operator_manifest(infrastructure)  # type: ignore[arg-type]
