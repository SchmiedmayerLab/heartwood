# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Run the native-worker cases on the Kubernetes driver of a kind cluster.

`images` verifies the gateway artifacts, builds the gateway and controller images and the
two derived worker images of the Docker qualification (a symbolic link workspace image and
a replacement worker image), and checks that the symbolic link image's working directory is
a link to `/workspace`. It pushes these images and the worker image to the cluster's
registry, the replacement worker image into the worker's repository, and copies the
supervisor and sandbox runtime images of the OpenShell release there under their digests.
The platform chart is then installed with the references and the repository that `images`
wrote to `images.json`.
`run` launches one session through the platform's control service, runs the cases in a
pod that holds that session's gateway credentials, and retains the evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from qualification_evidence import (
    DRILLS,
    KUBERNETES_CASES_NOT_RUN,
    KUBERNETES_PLATFORM_REQUIREMENTS,
    KUBERNETES_RETAINED,
    MANIFEST,
    cases,
    drill_results,
    failures,
    image_source,
    write_checksums,
)

ROOT = Path(__file__).resolve().parents[1]
_SESSIONS = "heartwood-sessions"
_PLATFORM = "heartwood-platform"
_CONTROL = "https://heartwood-control.heartwood-platform.svc.cluster.local:8443"
# A synthetic researcher and project of the chart's kind values.
_SUBJECT = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001"
_PROJECT = "demo"
_FIXTURE = "hw-qualification-fixture"
_PROVIDER = "hw-qualification-provider"
_CONTROLLER = "hw-qualification-controller"
_LAUNCHER = "hw-qualification-launcher"
# The conflict case labels the Sandbox resource whose deletions this webhook admits; the
# label and port are _DELETE_CONFLICT_LABEL and _DELETE_CONFLICT_PORT of its case file.
_CONFLICT_WEBHOOK = "hw-qualification-delete-conflict"
_CONFLICT_LABEL = "qualification.heartwood/delete-conflict"
_CONFLICT_PORT = 19674
_PYTHON = "/opt/heartwood/.venv/bin/python"
_SOURCES = (
    "deploy/qualify-kubernetes-worker.py",
    "deploy/qualify-native-worker.py",
    "deploy/qualification_evidence.py",
    "deploy/worker_model_profile.py",
    "deploy/tests/test_worker_kubernetes_integration.py",
    "deploy/tests/worker_kubernetes_fixture.py",
    "deploy/tests/gke_session_receipt.py",
    "deploy/tests/test_worker_native_integration.py",
    "deploy/tests/test_worker_lifecycle_integration.py",
    "deploy/tests/worker_native_fixture.py",
    "deploy/tests/worker_telemetry.py",
    "deploy/tests/worker_hostile_probe.py",
    "deploy/tests/worker_network_probe.py",
    "deploy/tests/worker_network_qualification.py",
    "deploy/tests/checkpoint_records_integration.py",
    "deploy/tests/checkpoint_transport_integration.py",
    "packages/audit/tests/tls_fixture.py",
    "images/worker/candidate/kubernetes-controller.Dockerfile",
    "images/worker/candidate/symlink-workspace.Dockerfile",
    "images/worker/candidate/replacement-worker.Dockerfile",
    "reference-platform/cluster/push-image.sh",
    "reference-platform/tools/oci_identity.py",
    "packages/gateway/src/heartwood/gateway/worker_image_identity.py",
)
# The exit status of the launch script when its input reached it incomplete.
_INCOMPLETE = 75
# The token and the platform CA certificate arrive on standard input, which can arrive
# incomplete, so the script refuses input of another length; the request trusts that CA
# alone.
_LAUNCH = """import json,ssl,sys,urllib.error,urllib.request
raw=sys.stdin.read()
if len(raw.encode())!=int(sys.argv[3]): sys.exit(75)
given=json.loads(raw)
request=urllib.request.Request(sys.argv[2],method=sys.argv[1],
 headers={'Authorization':'Bearer '+given['token']})
context=ssl.create_default_context(cadata=given['ca'])
try:
 with urllib.request.urlopen(request,timeout=300,context=context) as response:
  print(json.dumps({'status':response.status,'body':response.read().decode()}))
except urllib.error.HTTPError as error:
 print(json.dumps({'status':error.code,'body':error.read().decode()}))
"""


def _native() -> Any:  # noqa: ANN401
    """Load the Docker qualification's runner, which owns the shared build steps."""
    spec = importlib.util.spec_from_file_location(
        "native_qualification", ROOT / "deploy/qualify-native-worker.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _output(arguments: list[str], *, stdin: str | None = None) -> str:
    return subprocess.run(
        arguments, input=stdin, capture_output=True, text=True, check=True, timeout=600
    ).stdout


class Cluster:
    """Administrator access to the kind cluster, for what the platform would provide."""

    def __init__(self, name: str) -> None:
        """Select the kubectl context that kind wrote for the cluster."""
        self.name = name

    def kubectl(self, *arguments: str, stdin: str | None = None) -> str:
        """Run one kubectl command and return its output."""
        return _output(["kubectl", "--context", "kind-" + self.name, *arguments], stdin=stdin)

    def apply(self, *documents: dict[str, Any]) -> None:
        """Create or update objects."""
        self.kubectl(
            "apply",
            "--filename=-",
            stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(documents)}),
        )

    def json(self, *arguments: str) -> Any:  # noqa: ANN401
        """Read objects as the API reports them."""
        return json.loads(self.kubectl("get", *arguments, "--output=json"))


def _source_hashes(native: Any) -> dict[str, str]:  # noqa: ANN401
    return {name: native.digest(ROOT / name) for name in _SOURCES}


def images(arguments: argparse.Namespace) -> None:
    """Build what the cluster runs and push it to the cluster's registry."""
    native = _native()
    artifacts, output = arguments.gateway_artifacts.resolve(), arguments.output.resolve()
    manifest, record = native.verified_gateway(artifacts)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started_at = native.timestamp()
    sources = _source_hashes(native)
    identifiers, image_sources = {}, {}
    for name, image in (("worker", arguments.worker_image), ("runtime", arguments.runtime_image)):
        inspected = json.loads(_output(["docker", "image", "inspect", image]))[0]
        identifiers[name] = inspected["Id"]
        image_sources[name] = image_source(
            inspected, native.embedded_build_records(inspected["Id"])
        )
    published = {}
    if arguments.published_gateway:
        published = {"worker": arguments.worker_image, "gateway": arguments.published_gateway}
        identifiers["gateway"] = _output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", arguments.published_gateway]
        ).strip()
    else:
        identifiers["gateway"] = native.gateway_image(artifacts, manifest, record, output)
    native.run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--file",
            str(ROOT / "images/worker/candidate/kubernetes-controller.Dockerfile"),
            "--build-arg",
            "RUNTIME_IMAGE=" + arguments.runtime_image,
            "--build-context",
            "worker-qualification-tools=" + str(artifacts),
            "--build-context",
            "records-tls-fixture=" + str(ROOT / "packages/audit/tests"),
            "--iidfile",
            str(output / "controller-image-id"),
            str(ROOT),
        ]
    )
    identifiers["controller"] = (output / "controller-image-id").read_text().strip()
    # The two derived worker images of the Docker qualification: a workspace that is a
    # symbolic link, and a distinct image identity with unchanged contents.
    for name, dockerfile, context in (
        ("symlink_workspace", "symlink-workspace.Dockerfile", ROOT),
        ("replacement_worker", "replacement-worker.Dockerfile", ROOT / "images/worker/candidate"),
    ):
        native.run(
            [
                "docker",
                "buildx",
                "build",
                "--load",
                "--network",
                "none",
                "--file",
                str(ROOT / "images/worker/candidate" / dockerfile),
                "--build-arg",
                "WORKER_IMAGE=" + arguments.worker_image,
                "--iidfile",
                str(output / f"{name}-image-id"),
                str(context),
            ]
        )
        identifiers[name] = (output / f"{name}-image-id").read_text().strip()
    configured = json.loads(
        _output(["docker", "image", "inspect", identifiers["symlink_workspace"]])
    )
    symlink = _output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            _PYTHON,
            identifiers["symlink_workspace"],
            "-I",
            "-c",
            "from pathlib import Path;p=Path('/workspace-link');"
            "assert p.is_symlink() and p.resolve()==Path('/workspace');print('symlink')",
        ]
    ).strip()
    if configured[0]["Config"]["WorkingDir"] != "/workspace-link" or symlink != "symlink":
        raise RuntimeError("The symbolic link workspace image has no linked working directory")
    interpreter = _output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            _PYTHON,
            identifiers["worker"],
            "-I",
            "-c",
            "from pathlib import Path;import sys;print(Path(sys.executable).resolve())",
        ]
    ).strip()
    references = {}
    for name, repository in (
        ("worker", "heartwood-worker"),
        ("gateway", "openshell-gateway"),
        ("controller", "heartwood-qualification-controller"),
        ("symlink_workspace", "heartwood-symlink-workspace"),
        # The replacement shares the worker's repository, as a new image of one deployment.
        ("replacement_worker", "heartwood-worker"),
    ):
        if name in published:
            references[name] = published[name]
            continue
        # The registry names a repository after the image, so each image gets its own tag.
        tag = f"{repository}:qualification-{identifiers[name].removeprefix('sha256:')[:12]}"
        native.run(["docker", "tag", identifiers[name], tag])
        references[name] = _output(
            ["bash", str(ROOT / "reference-platform/cluster/push-image.sh"), arguments.cluster, tag]
        ).strip()
    # The supervisor and sandbox runtime images of the OpenShell release, copied under their
    # digests, so the cluster pulls every image of a worker from its own registry.
    for name in ("supervisor", "sandbox"):
        references[name] = _output(
            [
                "bash",
                str(ROOT / "reference-platform/cluster/push-image.sh"),
                arguments.cluster,
                manifest["images"][name],
                "openshell/" + name,
            ]
        ).strip()
    (output / "images.json").write_text(
        json.dumps(
            {
                "started_at": started_at,
                "invocation": {
                    "cluster": arguments.cluster,
                    "worker_image": arguments.worker_image,
                    "runtime_image": arguments.runtime_image,
                    "gateway_artifacts": str(artifacts),
                },
                "qualification_sources": sources,
                "images": identifiers,
                "image_references": references,
                # The value of the chart's images.openshellRepository.
                "openshell_repository": references["supervisor"].rpartition("/supervisor@")[0],
                "image_sources": image_sources,
                "gateway_build": record,
                "worker_interpreter": interpreter,
            },
            indent=2,
        )
        + "\n"
    )


def _pod(name: str, image: str, **spec: Any) -> dict[str, Any]:  # noqa: ANN401
    """A pod of the controller image that waits for commands."""
    container: dict[str, Any] = {
        "name": "controller",
        "image": image,
        "command": ["/bin/sleep", "7200"],
        "workingDir": "/qualification/output",
        # The cases write below the image's own output directory, so the root file
        # system stays writable.
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "resources": {
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": {"memory": "3Gi"},
        },
        "volumeMounts": spec.pop("volumeMounts", []),
        "env": spec.pop("env", []),
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": _SESSIONS, "labels": spec.pop("labels", {})},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "terminationGracePeriodSeconds": 1,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [container],
            "volumes": spec.pop("volumes", []),
            **spec,
        },
    }


def _launch(cluster: Cluster, method: str) -> dict[str, Any]:
    """Call the control service as the spawner would, from inside the cluster."""
    token = cluster.kubectl(
        "create", "token", "hub", "--namespace", _PLATFORM, "--audience=heartwood-control"
    )
    token = token.strip()
    reply = _call(cluster, method, token)
    attempts = 1
    # A launch is idempotent, and the spawner repeats one that the control service answers
    # with 502. The number of requests is recorded with the cluster.
    while method == "PUT" and reply["status"] == 502 and attempts < 3:
        time.sleep(5)
        reply = _call(cluster, method, token)
        attempts += 1
    if reply["status"] not in {200, 204}:
        raise RuntimeError(f"The control service answered {reply['status']}: {reply['body']}")
    launched: dict[str, Any] = json.loads(reply["body"]) if reply["body"] else {}
    return {**launched, "launch_requests": attempts}


def _researcher(cluster: Cluster) -> str:
    """Return the researcher identifier that names the synthetic researcher in platform paths.

    The platform derives it from the issuer and the subject under the key of the Secret
    heartwood-researcher-ids, so the subject itself appears in no URL.
    """
    hub = cluster.json("configmap", "heartwood-hub", "--namespace", _PLATFORM)
    issuer = json.loads(hub["data"]["settings.json"])["issuer"]
    secret = cluster.json("secret", "heartwood-researcher-ids", "--namespace", _PLATFORM)
    key = base64.b64decode(secret["data"]["key"]).decode().removesuffix("\n").encode()
    digest = hmac.new(key, f"{issuer}\0{_SUBJECT}".encode(), hashlib.sha256).hexdigest()
    return f"r{digest[:32]}"


def _call(cluster: Cluster, method: str, token: str) -> dict[str, Any]:
    """Send one launch request; input that reached the pod incomplete is sent again."""
    authority = cluster.json("configmap", "platform-ca", "--namespace", _PLATFORM)["data"]["ca.crt"]
    researcher = _researcher(cluster)
    payload = json.dumps({"token": token.strip(), "ca": authority})
    for attempt in range(1, 6):
        try:
            output = cluster.kubectl(
                "exec",
                "--stdin",
                "--namespace",
                _SESSIONS,
                _LAUNCHER,
                "--",
                _PYTHON,
                "-I",
                "-c",
                _LAUNCH,
                method,
                f"{_CONTROL}/v1/researchers/{researcher}/projects/{_PROJECT}/launch",
                str(len(payload.encode())),
                stdin=payload,
            )
        except subprocess.CalledProcessError as error:
            if error.returncode != _INCOMPLETE:
                raise
            sys.stderr.write(f"The launch input reached the pod incomplete (attempt {attempt})\n")
            continue
        reply: dict[str, Any] = json.loads(output)
        return reply
    raise RuntimeError("The launch input did not reach the pod complete")


def _provider_certificate(cluster: Cluster, host: str) -> dict[str, str]:
    """Issue the controller's listeners a certificate from the platform's authority.

    The synthetic model route uses the full host name; the API server calls the admission
    webhook at the Service's name without the cluster domain.
    """
    authority = cluster.json("secret", "platform-ca", "--namespace", _PLATFORM)["data"]
    with TemporaryDirectory(prefix="qualification-ca-") as directory:
        work = Path(directory)
        for name in ("tls.crt", "tls.key"):
            (work / name).write_bytes(base64.b64decode(authority[name]))
            (work / name).chmod(0o600)
        (work / "extensions").write_text(
            f"subjectAltName=DNS:{host},DNS:{host.removesuffix('.cluster.local')}\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
        )
        _output(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(work / "server.key"),
                "-out",
                str(work / "server.csr"),
                "-subj",
                "/CN=" + host,
            ]
        )
        _output(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(work / "server.csr"),
                "-CA",
                str(work / "tls.crt"),
                "-CAkey",
                str(work / "tls.key"),
                "-CAcreateserial",
                "-days",
                "2",
                "-sha256",
                "-extfile",
                str(work / "extensions"),
                "-out",
                str(work / "server.crt"),
            ]
        )
        return {
            "tls.crt": (work / "server.crt").read_text(),
            "tls.key": (work / "server.key").read_text(),
            "ca.crt": (work / "tls.crt").read_text(),
        }


def _secret(name: str, data: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": _SESSIONS},
        "type": "Opaque",
        "stringData": data,
    }


def _researcher_password(cluster: Cluster) -> str:
    """Return the synthetic password of the session's researcher, researcher-one."""
    secret = cluster.json("secret", "keycloak-researchers", "--namespace", _PLATFORM)
    return base64.b64decode(secret["data"]["researcher-one"]).decode()


def _fixture_objects(cluster: Cluster, launched: dict[str, Any], built: dict[str, Any]) -> None:
    """Create the controller pod and what only the qualification adds around a session."""
    namespace, session = launched["namespace"], launched["session"]
    host = f"{_PROVIDER}.{_SESSIONS}.svc.cluster.local"
    rbac = "rbac.authorization.k8s.io"
    account = {"kind": "ServiceAccount", "name": _FIXTURE, "namespace": _SESSIONS}
    selector = {"heartwood.qualification": "controller"}
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": _FIXTURE, "namespace": _SESSIONS},
        },
        # The outside observer of the cases: it reads the session's objects, patches the
        # Sandbox resource for the injected conflict, adds and removes policies, and
        # runs one probe pod.
        {
            "apiVersion": f"{rbac}/v1",
            "kind": "Role",
            "metadata": {"name": _FIXTURE, "namespace": namespace},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "persistentvolumeclaims", "events", "services"],
                    "verbs": ["get", "list"],
                },
                # One probe pod with a workload pod's labels, and its output.
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "delete"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                {
                    "apiGroups": ["agents.x-k8s.io"],
                    "resources": ["sandboxes"],
                    "verbs": ["get", "list", "patch"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["networkpolicies"],
                    "verbs": ["get", "list", "create", "delete"],
                },
            ],
        },
        {
            "apiVersion": f"{rbac}/v1",
            "kind": "RoleBinding",
            "metadata": {"name": _FIXTURE, "namespace": namespace},
            "roleRef": {"apiGroup": rbac, "kind": "Role", "name": _FIXTURE},
            "subjects": [account],
        },
        {
            "apiVersion": f"{rbac}/v1",
            "kind": "ClusterRole",
            "metadata": {"name": _FIXTURE},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["namespaces"],
                    "resourceNames": [namespace],
                    "verbs": ["get"],
                }
            ],
        },
        {
            "apiVersion": f"{rbac}/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": _FIXTURE},
            "roleRef": {"apiGroup": rbac, "kind": "ClusterRole", "name": _FIXTURE},
            "subjects": [account],
        },
    )
    token = cluster.kubectl(
        "create", "token", _FIXTURE, "--namespace", _SESSIONS, "--duration=2h"
    ).strip()
    sandbox = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
    }
    certificate = _provider_certificate(cluster, host)
    cluster.apply(
        # The session's researcher signs in at the project client in the catalog case.
        _secret(_FIXTURE, {"token": token, "researcher-password": _researcher_password(cluster)}),
        _secret(_PROVIDER, certificate),
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": _PROVIDER, "namespace": _SESSIONS},
            "spec": {
                "selector": selector,
                "ports": [
                    {"name": "model", "port": 19671, "targetPort": 19671},
                    {"name": "conflict", "port": _CONFLICT_PORT, "targetPort": _CONFLICT_PORT},
                ],
            },
        },
        # The conflict case changes the Sandbox resource while the API server admits a
        # deletion of it, so the deletion meets a conflict on every run. Only a Sandbox
        # resource of the session that the case labeled reaches the controller, and the
        # API server admits the deletion when the controller does not answer, so the
        # case fails on the missing conflict rather than leaving a resource behind. The
        # kind cluster's API server runs on the controller pod's node, and Calico admits
        # connections from a pod's own node, so the controller's ingress policy admits it.
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingWebhookConfiguration",
            "metadata": {"name": _CONFLICT_WEBHOOK},
            "webhooks": [
                {
                    "name": "delete-conflict.qualification.heartwood",
                    "clientConfig": {
                        "service": {
                            "namespace": _SESSIONS,
                            "name": _PROVIDER,
                            "port": _CONFLICT_PORT,
                            "path": "/delete",
                        },
                        "caBundle": base64.b64encode(certificate["ca.crt"].encode()).decode(),
                    },
                    "rules": [
                        {
                            "apiGroups": ["agents.x-k8s.io"],
                            "apiVersions": ["*"],
                            "resources": ["sandboxes"],
                            "operations": ["DELETE"],
                            "scope": "Namespaced",
                        }
                    ],
                    "namespaceSelector": sandbox["namespaceSelector"],
                    "objectSelector": {"matchLabels": {_CONFLICT_LABEL: "armed"}},
                    "failurePolicy": "Ignore",
                    "sideEffects": "NoneOnDryRun",
                    "admissionReviewVersions": ["v1"],
                    "timeoutSeconds": 10,
                }
            ],
        },
        # The synthetic model route stands where the platform's model service would. The
        # canary port admits every pod of the session namespace, so only the confinement
        # of a worker keeps the worker from it.
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": _PROVIDER, "namespace": _SESSIONS},
            "spec": {
                "podSelector": {"matchLabels": selector},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [sandbox],
                        "ports": [
                            {"protocol": "TCP", "port": 19671},
                            {"protocol": "TCP", "port": 19672},
                        ],
                    }
                ],
            },
        },
        _pod(
            _CONTROLLER,
            built["image_references"]["controller"],
            serviceAccountName=launched["gateway_service_account"],
            automountServiceAccountToken=True,
            # The session label admits this pod to the session's sandbox gateway.
            labels={**selector, "heartwood.platform/session": session},
            env=[
                {"name": name, "value": value}
                for name, value in {
                    "HW_QUALIFICATION_OUTPUT": "/qualification/output",
                    "HW_SESSION_NAMESPACE": namespace,
                    **(
                        {"HW_RUNTIME_MANIFESTS": json.dumps(built["runtime_manifests"])}
                        if "runtime_manifests" in built
                        else {}
                    ),
                    "HW_WORKER_IMAGE": built["image_references"]["worker"],
                    "HW_GATEWAY_IMAGE": built["image_references"]["gateway"],
                    "HW_SUPERVISOR_IMAGE": built["image_references"]["supervisor"],
                    "HW_SANDBOX_IMAGE": built["image_references"]["sandbox"],
                    "HW_SYMLINK_WORKSPACE_IMAGE": built["image_references"]["symlink_workspace"],
                    "HW_REPLACEMENT_WORKER_IMAGE": built["image_references"]["replacement_worker"],
                    "HW_OPENSHELL_ENDPOINT": launched["openshell_endpoint"],
                    "HW_PROVIDER_HOST": host,
                    "HW_WORKER_INTERPRETER": built["worker_interpreter"],
                    "OPENHANDS_SUPPRESS_BANNER": "1",
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                }.items()
            ],
            volumeMounts=[
                {"name": "gateway", "mountPath": "/qualification/gateway", "readOnly": True},
                {"name": "fixture", "mountPath": "/qualification/fixture", "readOnly": True},
                {"name": "provider", "mountPath": "/qualification/provider", "readOnly": True},
            ],
            volumes=[
                {
                    "name": "gateway",
                    "secret": {
                        "secretName": launched["gateway_service_account"],
                        "defaultMode": 0o440,
                    },
                },
                {"name": "fixture", "secret": {"secretName": _FIXTURE, "defaultMode": 0o440}},
                {
                    "name": "provider",
                    "secret": {
                        "secretName": _PROVIDER,
                        "defaultMode": 0o440,
                        "items": [
                            {"key": "tls.crt", "path": "server/tls.crt"},
                            {"key": "tls.key", "path": "server/tls.key"},
                            {"key": "ca.crt", "path": "ca.crt"},
                        ],
                    },
                },
            ],
        ),
    )
    cluster.kubectl(
        "wait",
        "--namespace",
        _SESSIONS,
        "--for=condition=Ready",
        "--timeout=300s",
        "pod/" + _CONTROLLER,
    )


def _cluster_record(cluster: Cluster, launched: dict[str, Any]) -> dict[str, Any]:
    """Describe the cluster and the session namespace the cases ran in."""
    nodes = cluster.json("nodes")["items"]
    plugin = cluster.json("daemonset", "calico-node", "--namespace=kube-system")
    namespace = launched["namespace"]
    pulls = [
        {"object": event["involvedObject"]["name"], "message": event["message"]}
        for event in cluster.json("events", "--namespace", namespace)["items"]
        if event.get("reason") == "Pulled"
    ]
    return {
        "cluster": cluster.name,
        "kubernetes": json.loads(cluster.kubectl("version", "--output=json"))["serverVersion"][
            "gitVersion"
        ],
        "network_plugin": plugin["spec"]["template"]["spec"]["containers"][0]["image"],
        "nodes": [
            {
                "name": node["metadata"]["name"],
                **{
                    name: node["status"]["nodeInfo"][name]
                    for name in (
                        "operatingSystem",
                        "architecture",
                        "kernelVersion",
                        "osImage",
                        "containerRuntimeVersion",
                        "kubeletVersion",
                    )
                },
            }
            for node in nodes
        ],
        "session": launched["session"],
        "launch_requests": launched["launch_requests"],
        "namespace": namespace,
        "namespace_uid": launched["namespace_uid"],
        "kube_system_uid": launched["kube_system_uid"],
        "gateway_service_account": launched["gateway_service_account"],
        "namespace_labels": cluster.json("namespace", namespace)["metadata"]["labels"],
        "network_policies": sorted(
            item["metadata"]["name"]
            for item in cluster.json("networkpolicies", "--namespace", namespace)["items"]
        ),
        "image_pulls": pulls,
    }


def _remove(cluster: Cluster, namespace: str | None) -> bool:
    """Remove the fixture's objects and the session; report whether the namespace is gone."""
    removed = False
    try:
        if namespace is not None:
            _launch(cluster, "DELETE")
            cluster.kubectl("wait", "--for=delete", "namespace/" + namespace, "--timeout=300s")
            removed = True
    finally:
        try:
            cluster.kubectl(
                "delete",
                "--namespace",
                _SESSIONS,
                "--ignore-not-found",
                "--wait=false",
                f"pod/{_CONTROLLER}",
                f"pod/{_LAUNCHER}",
                f"service/{_PROVIDER}",
                f"networkpolicy/{_PROVIDER}",
                f"secret/{_PROVIDER}",
                f"secret/{_FIXTURE}",
                f"serviceaccount/{_FIXTURE}",
            )
        finally:
            cluster.kubectl(
                "delete",
                "--ignore-not-found",
                f"clusterrolebinding/{_FIXTURE}",
                f"clusterrole/{_FIXTURE}",
                f"validatingwebhookconfiguration/{_CONFLICT_WEBHOOK}",
            )
    return removed


def run(arguments: argparse.Namespace) -> None:
    """Run the cases in a provisioned session and retain what they observed."""
    native = _native()
    output = arguments.output.resolve()
    built = json.loads((output / "images.json").read_text())
    if getattr(arguments, "runtime_manifests", None):
        built["runtime_manifests"] = json.loads(arguments.runtime_manifests.read_text())
    cluster = Cluster(arguments.cluster)
    host = json.loads(
        _output(
            [
                "docker",
                "info",
                "--format",
                '{"kernel":{{json .KernelVersion}},"os":{{json .OSType}},'
                '"architecture":{{json .Architecture}},"docker_version":{{json .ServerVersion}}}',
            ]
        )
    )
    source = {
        "commit": _output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip(),
        "tree_clean": not _output(["git", "-C", str(ROOT), "status", "--porcelain"]).strip(),
    }
    namespace = None
    record: dict[str, Any] = {}
    returncode = 1
    try:
        # Pods of an earlier run that did not finish would keep their old image.
        cluster.kubectl(
            "delete", "--namespace", _SESSIONS, "--ignore-not-found", "--wait",
            f"pod/{_CONTROLLER}", f"pod/{_LAUNCHER}",
        )  # fmt: skip
        cluster.apply(
            _pod(_LAUNCHER, built["image_references"]["controller"]),
        )
        cluster.kubectl(
            "wait", "--namespace", _SESSIONS, "--for=condition=Ready", "--timeout=300s",
            "pod/" + _LAUNCHER,
        )  # fmt: skip
        launched = _launch(cluster, "PUT")
        namespace = launched["namespace"]
        _fixture_objects(cluster, launched, built)
        with (output / "qualification.log").open("w") as log:
            returncode = subprocess.run(
                [
                    "kubectl",
                    "--context",
                    "kind-" + cluster.name,
                    "exec",
                    "--namespace",
                    _SESSIONS,
                    _CONTROLLER,
                    "--",
                    _PYTHON,
                    "-I",
                    "-B",
                    "-m",
                    "pytest",
                    "-o",
                    "addopts=",
                    "-o",
                    "pythonpath=/opt/qualification",
                    "-p",
                    "no:cacheprovider",
                    "-v",
                    "--junitxml=/qualification/output/junit.xml",
                    "/opt/qualification/tests/test_worker_kubernetes_integration.py",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        # Private directories hold generated credentials and stay in the pod.
        archive = subprocess.run(
            [
                "kubectl",
                "--context",
                "kind-" + cluster.name,
                "exec",
                "--namespace",
                _SESSIONS,
                _CONTROLLER,
                "--",
                "tar",
                "--create",
                "--file=-",
                "--directory=/qualification/output",
                "--exclude=private",
                ".",
            ],
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as retained:
            retained.extractall(output, filter="data")
        record = _cluster_record(cluster, launched)
        (output / "gateway.log").write_text(
            cluster.kubectl("logs", "--namespace", namespace, "openshell-0", "--tail=-1")
        )
    finally:
        try:
            removed = _remove(cluster, namespace)
        except subprocess.CalledProcessError as error:
            # The evidence records the failed removal; the case error stays the one raised.
            sys.stderr.write(str(error.stderr or ""))
            removed = False
    (output / "cluster.json").write_text(json.dumps(record, indent=2) + "\n")
    evidence: dict[str, Any] = {
        "qualification_kind": "synthetic-kubernetes",
        **(
            {
                "workflow_run": {
                    "id": int(os.environ["GITHUB_RUN_ID"]),
                    "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                }
            }
            if "GITHUB_RUN_ID" in os.environ
            else {}
        ),
        "driver": "openshell-kubernetes",
        "release_version": tomllib.loads((ROOT / "VERSION.toml").read_text())["version"],
        "source": source,
        "started_at": built["started_at"],
        "finished_at": native.timestamp(),
        "invocation": {**built["invocation"], "output": str(output)},
        "requested_model_profile": None,
        "host": host,
        "cluster": {name: record.get(name) for name in ("kubernetes", "network_plugin", "nodes")},
        "kubernetes_identities": {
            name: record.get(name)
            for name in (
                "session",
                "namespace",
                "namespace_uid",
                "kube_system_uid",
                "gateway_service_account",
            )
        },
        "qualification_sources": built["qualification_sources"],
        "qualification_sources_unchanged": built["qualification_sources"] == _source_hashes(native),
        "images": built["images"],
        "image_references": built["image_references"],
        "image_sources": built["image_sources"],
        "gateway_build": built["gateway_build"],
        "returncode": returncode,
        "session_removed": removed,
        "cases": cases(output / "junit.xml"),
        "platform_requirements": list(KUBERNETES_PLATFORM_REQUIREMENTS),
        "cases_not_run": KUBERNETES_CASES_NOT_RUN,
        "sensitive_data_ready": False,
    }
    evidence["failures"] = failures(evidence)
    evidence["passed"] = not evidence["failures"]
    (output / MANIFEST).write_text(json.dumps(evidence, indent=2) + "\n")
    (output / DRILLS).write_text(json.dumps(drill_results(output), indent=2) + "\n")
    write_checksums(output, KUBERNETES_RETAINED)
    if evidence["failures"]:
        raise RuntimeError(
            "Kubernetes qualification failed: "
            + "; ".join(evidence["failures"])
            + "; inspect qualification.log and junit.xml"
        )


def main() -> None:
    """Prepare images or run the Kubernetes qualification."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("images", help=images.__doc__)
    build.add_argument(
        "--published-gateway", help="Use the published gateway and worker references"
    )
    build.add_argument("--worker-image", required=True)
    build.add_argument("--runtime-image", required=True)
    build.add_argument("--gateway-artifacts", required=True, type=Path)
    build.set_defaults(function=images)
    execute = commands.add_parser("run", help=run.__doc__)
    execute.add_argument("--runtime-manifests", type=Path)
    execute.set_defaults(function=run)
    for command in (build, execute):
        command.add_argument("--cluster", required=True, help="Name of the kind cluster")
        command.add_argument("--output", required=True, type=Path)
    selected = parser.parse_args()
    try:
        selected.function(selected)
    except subprocess.CalledProcessError as error:
        sys.stderr.write(str(error.stderr or ""))
        raise


if __name__ == "__main__":
    main()
