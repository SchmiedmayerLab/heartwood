# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Kubernetes admission accepts only the listed pod, policy and namespace values."""

from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from heartwood.core_adapter.worker_attempts import WorkerAttemptError, WorkerAttemptIntent
from heartwood.gateway.worker_deployment import KubernetesWorkerDeployment
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.gateway.worker_kubernetes import KubernetesWorkerAdmission, admission_record

_IMAGE = "sha256:" + "a" * 64
_SUPERVISOR_IMAGE = "sha256:" + "5" * 64
_SANDBOX_IMAGE = "sha256:" + "6" * 64
_NAMESPACE = "hw-s-synthetic"
_SANDBOX_ID = "0698f582-bdd8-4ddc-969c-96c0e749a85e"
_SANDBOX_UID = "52114edd-79aa-4545-a2bb-ea26c838c5f6"
_ATTEMPT_KEY = "synthetic-attempt-key"
_PAIR = "openshell.ai/boundary-pair"
_ROLE = "openshell.ai/boundary-role"
_DELETE = object()

Objects = dict[str, dict[str, Any]]


def _settings(root: Path, **changes: object) -> KubernetesWorkerDeployment:
    return KubernetesWorkerDeployment.model_validate(
        {
            "driver": "openshell-kubernetes",
            "gateway": "synthetic",
            "native_config_home": root / "native",
            "openshell_cli": root / "openshell",
            "kubectl_cli": root / "kubectl",
            "kubeconfig": root / "kubeconfig",
            "namespace": _NAMESPACE,
            "image_repository": "registry.test/heartwood/worker",
            "supervisor_image_digest": _SUPERVISOR_IMAGE,
            "sandbox_image_digest": _SANDBOX_IMAGE,
            "workspace": "hw",
            "image_digest": _IMAGE,
            "sandbox_policy_sha256": "b" * 64,
            "policy_path": root / "policy.json",
            "policy_file_sha256": "c" * 64,
            "profile_id": "synthetic",
            "model": "synthetic",
            "model_endpoint": "https://provider.test/v1/chat/completions",
            "provider": "synthetic",
            "placeholder_env": "SYNTHETIC_PLACEHOLDER",
            "inputs": WorkerInputManifest(),
            "cpu": "1",
            "memory": "2Gi",
            **changes,
        }
    )


@pytest.mark.parametrize("workspace", ["hw-staging", "hw-staging-a"])
def test_a_workspace_named_like_a_staging_pod_is_refused(tmp_path: Path, workspace: str) -> None:
    # A workload pod of such a workspace would be named `hw-staging-*`, the only pod name
    # the platform admits exec sessions into.
    with pytest.raises(ValueError, match="reserved for staging pods"):
        _settings(tmp_path, workspace=workspace)
    assert _settings(tmp_path, workspace="hw-stagings").workspace == "hw-stagings"


def _intent() -> WorkerAttemptIntent:
    return WorkerAttemptIntent(
        project_id=uuid4(),
        session_id="synthetic",
        attempt_id=uuid4(),
        conversation_id=uuid4(),
        image_digest=_IMAGE,
        gateway_policy_sha256="b" * 64,
        sandbox_policy_sha256="c" * 64,
        input_manifest_sha256="d" * 64,
    )


@pytest.fixture
def admission(tmp_path: Path) -> KubernetesWorkerAdmission:
    return KubernetesWorkerAdmission(_settings(tmp_path), _intent())


def _restricted() -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
        "runAsGroup": 10001,
        "runAsNonRoot": True,
        "runAsUser": 10001,
    }


def _owner(*, controller: bool) -> dict[str, Any]:
    return {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "blockOwnerDeletion": controller,
        **({"controller": True} if controller else {}),
        "kind": "Sandbox",
        "name": "",
        "uid": _SANDBOX_UID,
    }


def _status(name: str, repository: str, digest: str) -> dict[str, Any]:
    return {"name": name, "image": f"{repository}@{digest}", "imageID": f"{repository}@{digest}"}


def _objects(admission: KubernetesWorkerAdmission) -> Objects:
    """One sandbox as the pinned driver creates it, with synthetic identities.

    The shapes follow objects captured from a local cluster: the workload pod and its
    supervisor pod, the two network policies, and a session namespace whose limit range gave
    the init containers default resources.
    """
    pod, worker, sandbox = admission.pod_name, "registry.test/heartwood/worker", _SANDBOX_ID
    runtime, supervisor = "ghcr.io/nvidia/openshell/sandbox", "ghcr.io/nvidia/openshell/supervisor"
    defaults = {
        "limits": {"cpu": "500m", "memory": "512Mi"},
        "requests": {"cpu": "100m", "memory": "128Mi"},
    }
    limits = {"cpu": "1", "memory": "2Gi"}
    workload = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "annotations": {"openshell.ai/sandbox-id": sandbox},
            "labels": {
                **admission.labels,
                "heartwood.role": "worker",
                _PAIR: sandbox,
                _ROLE: "workload",
            },
            "name": pod,
            "namespace": _NAMESPACE,
            "ownerReferences": [{**_owner(controller=True), "name": pod}],
            "uid": "58d9b510-70a1-417f-a88d-f5f9b5a957a1",
        },
        "spec": {
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "command": ["/.openshell/runtime/openshell-sandbox", "--bootstrap"],
                    "env": [
                        {"name": "SESSION_API_KEY", "value": _ATTEMPT_KEY},
                        {"name": "OPENSHELL_SANDBOX_UID", "value": "10001"},
                    ],
                    "image": f"{worker}@{_IMAGE}",
                    "name": "agent",
                    "resources": {"limits": dict(limits), "requests": dict(limits)},
                    "securityContext": {
                        key: value
                        for key, value in _restricted().items()
                        if key != "readOnlyRootFilesystem"
                    },
                    "volumeMounts": [
                        {"mountPath": "/inputs", "name": "inputs", "readOnly": True},
                        {"mountPath": "/skills", "name": "skills", "readOnly": True},
                        {
                            "mountPath": "/.openshell/runtime",
                            "name": "openshell-runtime",
                            "readOnly": True,
                        },
                        {"mountPath": "/.openshell/state", "name": "openshell-runtime-state"},
                        {
                            "mountPath": "/.openshell/pod-identity",
                            "name": "openshell-pod-identity",
                            "readOnly": True,
                        },
                        {"mountPath": "/run", "name": "openshell-run"},
                        {"mountPath": "/sandbox", "name": "workspace"},
                    ],
                }
            ],
            "dnsPolicy": "None",
            "initContainers": [
                {
                    "command": ["/openshell-sandbox", "bootstrap"],
                    "image": f"{runtime}@{_SANDBOX_IMAGE}",
                    "name": "openshell-sandbox-bootstrap",
                    "resources": copy.deepcopy(defaults),
                    "securityContext": _restricted(),
                    "volumeMounts": [
                        {
                            "mountPath": "/.openshell/bootstrap-input",
                            "name": "openshell-sandbox-bootstrap",
                            "readOnly": True,
                        },
                        {"mountPath": "/.openshell/runtime", "name": "openshell-runtime"},
                        {"mountPath": "/.openshell/state", "name": "openshell-runtime-state"},
                    ],
                },
                {
                    "command": ["/.openshell/runtime/openshell-sandbox", "seed-workspace"],
                    "image": f"{worker}@{_IMAGE}",
                    "name": "workspace-init",
                    "resources": copy.deepcopy(defaults),
                    "securityContext": _restricted(),
                    "volumeMounts": [
                        {"mountPath": "/mnt/openshell-workspace", "name": "workspace"},
                        {
                            "mountPath": "/.openshell/runtime",
                            "name": "openshell-runtime",
                            "readOnly": True,
                        },
                    ],
                },
            ],
            "securityContext": {
                "fsGroup": 10001,
                "fsGroupChangePolicy": "OnRootMismatch",
                "runAsGroup": 10001,
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
                "supplementalGroupsPolicy": "Strict",
            },
            "restartPolicy": "Never",
            "serviceAccountName": "openshell-sandbox",
            "shareProcessNamespace": False,
            "volumes": [
                {
                    "name": mount,
                    "persistentVolumeClaim": {"claimName": claim, "readOnly": True},
                }
                for mount, claim in admission.names.items()
            ]
            + [
                {"emptyDir": {"medium": "Memory"}, "name": "openshell-runtime"},
                {"emptyDir": {"medium": "Memory"}, "name": "openshell-runtime-state"},
                {
                    "name": "openshell-sandbox-bootstrap",
                    "secret": {"defaultMode": 288, "secretName": f"os-sandbox-{sandbox}-f8c054"},
                },
                {
                    "downwardAPI": {
                        "items": [{"fieldRef": {"fieldPath": "metadata.uid"}, "path": "uid"}]
                    },
                    "name": "openshell-pod-identity",
                },
                {"emptyDir": {"medium": "Memory"}, "name": "openshell-run"},
                {"name": "workspace", "persistentVolumeClaim": {"claimName": "workspace-" + pod}},
            ],
        },
        "status": {
            "containerStatuses": [_status("agent", worker, _IMAGE)],
            "initContainerStatuses": [
                _status("openshell-sandbox-bootstrap", runtime, _SANDBOX_IMAGE),
                _status("workspace-init", worker, _IMAGE),
            ],
            "phase": "Running",
        },
    }
    supervisor_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "labels": {
                _PAIR: sandbox,
                _ROLE: "supervisor",
                "openshell.ai/component": "supervisor",
                "openshell.ai/sandbox-id": sandbox,
            },
            "name": "os-supervisor-" + sandbox,
            "namespace": _NAMESPACE,
            "ownerReferences": [{**_owner(controller=False), "name": pod}],
        },
        "spec": {
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "command": ["/openshell-supervisor"],
                    "env": [{"name": "OPENSHELL_SANDBOX_ID", "value": sandbox}],
                    "image": f"{supervisor}@{_SUPERVISOR_IMAGE}",
                    "name": "supervisor",
                    "resources": copy.deepcopy(defaults),
                    "securityContext": _restricted(),
                    "volumeMounts": [
                        {
                            "mountPath": "/.openshell/supervisor",
                            "name": "bootstrap",
                            "readOnly": True,
                        },
                        {
                            "mountPath": "/var/run/secrets/openshell",
                            "name": "sa-token",
                            "readOnly": True,
                        },
                        {"mountPath": "/run/openshell", "name": "run"},
                    ],
                }
            ],
            "securityContext": {"fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "serviceAccountName": "openshell-sandbox",
            "volumes": [
                {"name": "bootstrap", "secret": {"secretName": f"os-supervisor-{sandbox}-f8c054"}},
                {
                    "name": "sa-token",
                    "projected": {
                        "sources": [
                            {
                                "serviceAccountToken": {
                                    "audience": "openshell-gateway",
                                    "expirationSeconds": 3600,
                                    "path": "token",
                                }
                            }
                        ]
                    },
                },
                {"emptyDir": {}, "name": "run"},
                {"emptyDir": {}, "name": "logs"},
                {"name": "client-tls", "secret": {"secretName": "openshell-client-tls"}},
            ],
        },
        "status": {
            "containerStatuses": [_status("supervisor", supervisor, _SUPERVISOR_IMAGE)],
            "phase": "Running",
        },
    }
    port = [{"port": 5500, "protocol": "TCP"}]
    control = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "labels": {"openshell.ai/sandbox-id": sandbox},
            "name": "os-control-" + sandbox,
            "namespace": _NAMESPACE,
            "ownerReferences": [{**_owner(controller=False), "name": pod}],
        },
        "spec": {
            "egress": [
                {
                    "ports": port,
                    "to": [{"podSelector": {"matchLabels": {_PAIR: sandbox, _ROLE: "supervisor"}}}],
                }
            ],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {_PAIR: sandbox, _ROLE: "workload"}}}],
                    "ports": port,
                }
            ],
            "podSelector": {"matchLabels": {_PAIR: sandbox}},
            "policyTypes": ["Ingress", "Egress"],
        },
    }
    fence = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "openshell-sandbox-workloads", "namespace": _NAMESPACE},
        "spec": {
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {_ROLE: "supervisor"}}}],
                    "ports": port,
                }
            ],
            "podSelector": {"matchLabels": {_ROLE: "workload"}},
            "policyTypes": ["Ingress", "Egress"],
        },
    }
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "labels": {
                "kubernetes.io/metadata.name": _NAMESPACE,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
            },
            "name": _NAMESPACE,
        },
        "status": {"phase": "Active"},
    }
    return {
        f"pod/{pod}": workload,
        f"pod/os-supervisor-{sandbox}": supervisor_pod,
        f"networkpolicy.networking.k8s.io/os-control-{sandbox}": control,
        "networkpolicy.networking.k8s.io/openshell-sandbox-workloads": fence,
        f"namespace/{_NAMESPACE}": namespace,
    }


def _serve(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch, objects: Objects
) -> list[tuple[str, ...]]:
    """Answer `kubectl get` from the given objects; an absent object fails as the tool does."""
    calls: list[tuple[str, ...]] = []

    def command(*arguments: str) -> bytes:
        calls.append(arguments)
        assert arguments[0] == "get"
        if arguments[2:] == ("--output=json",):
            items = [value for key, value in objects.items() if key.startswith(arguments[1] + "/")]
            return json.dumps({"kind": "List", "items": items}).encode()
        assert arguments[3:] == ("--output=json",)
        key = f"{arguments[1]}/{arguments[2]}"
        if key not in objects:
            raise WorkerAttemptError("native Kubernetes operation requires reconciliation")
        return json.dumps(objects[key]).encode()

    monkeypatch.setattr(admission, "command", command)
    return calls


def _change(value: Any, path: tuple[Any, ...], new: object) -> None:  # noqa: ANN401
    for key in path[:-1]:
        value = value[key]
    if new is _DELETE:
        del value[path[-1]]
    else:
        value[path[-1]] = new


def _named(objects: Objects, prefix: str) -> dict[str, Any]:
    (found,) = [value for key, value in objects.items() if key.startswith(prefix)]
    return found


def _workload(objects: Objects) -> dict[str, Any]:
    return _named(objects, "pod/hw--")


def _supervisor(objects: Objects) -> dict[str, Any]:
    return _named(objects, "pod/os-supervisor-")


_CA_BUNDLE = {
    "name": "openshell-additional-ca",
    "configMap": {
        "defaultMode": 292,
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
        "name": "openshell-additional-ca-dc5cbc21a452-6e07edecaad1ecac6eff6a7cf2",
    },
}


def test_the_supervisor_may_mount_the_drivers_certificate_bundle(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    _supervisor(objects)["spec"]["volumes"].append(copy.deepcopy(_CA_BUNDLE))
    _serve(admission, monkeypatch, objects)
    admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("name",), "other"),
        (("configMap", "name"), "openshell-config"),
        (("configMap", "name"), "kube-root-ca.crt"),
        (("configMap", "items"), _DELETE),
        (("configMap", "items"), [{"key": "gateway.toml", "path": "ca.crt"}]),
        (("configMap", "items", 0, "path"), "../ca.crt"),
    ],
)
def test_any_other_config_map_volume_of_the_supervisor_is_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    volume = copy.deepcopy(_CA_BUNDLE)
    _change(volume, path, value)
    _supervisor(objects)["spec"]["volumes"].append(volume)
    _serve(admission, monkeypatch, objects)
    with pytest.raises(WorkerAttemptError, match="supervisor confinement does not match"):
        admission.inspect_worker(_SANDBOX_ID)


def _control(objects: Objects) -> dict[str, Any]:
    return _named(objects, "networkpolicy.networking.k8s.io/os-control-")


def _fence(objects: Objects) -> dict[str, Any]:
    return _named(objects, "networkpolicy.networking.k8s.io/openshell-")


def _namespace(objects: Objects) -> dict[str, Any]:
    return _named(objects, "namespace/")


def test_admission_accepts_the_driver_objects_and_reads_each_once(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    calls = _serve(admission, monkeypatch, objects)
    admission.inspect_worker(_SANDBOX_ID)
    listing = ("get", "networkpolicy.networking.k8s.io", "--output=json")
    assert calls.count(listing) == 1
    assert sorted(f"{call[1]}/{call[2]}" for call in calls if call != listing) == sorted(objects)
    assert all(call[0] == "get" for call in calls)


_AGENT = ("spec", "containers", 0)
_RUNTIME_INIT = ("spec", "initContainers", 0)
_WORKSPACE_INIT = ("spec", "initContainers", 1)
_SUPERVISOR = ("spec", "containers", 0)
_OMITTED_AT_DEFAULT = [
    (("spec", "hostNetwork"), False),
    (("spec", "hostPID"), False),
    (("spec", "hostIPC"), False),
    (("spec", "hostUsers"), True),
    ((*_AGENT, "securityContext", "privileged"), False),
]


@pytest.mark.parametrize("pod", [_workload, _supervisor])
@pytest.mark.parametrize(("path", "default"), _OMITTED_AT_DEFAULT)
def test_fields_the_api_server_omits_at_their_default_are_accepted_absent_or_at_that_default(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    pod: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    default: bool,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    # The driver's pods omit all five fields.
    admission.inspect_worker(_SANDBOX_ID)
    _change(pod(objects), path, default)
    admission.inspect_worker(_SANDBOX_ID)
    for other in (not default, None, "false", 0):
        _change(pod(objects), path, other)
        with pytest.raises(WorkerAttemptError, match="confinement does not match admission"):
            admission.inspect_worker(_SANDBOX_ID)


def test_the_driver_settings_that_weaken_nothing_are_accepted(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    workload = _workload(objects)
    # As the pinned driver reports them on its workload pod.
    workload["spec"]["securityContext"]["sysctls"] = [
        {"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"}
    ]
    workload["spec"]["containers"][0]["ports"] = [
        {"containerPort": 5500, "name": "sandbox-control", "protocol": "TCP"}
    ]
    workload["spec"]["containers"][0]["env"].append({"name": "OPENSHELL_OCI_IMAGE_USER"})
    workload["spec"]["containers"][0]["volumeMounts"][0]["recursiveReadOnly"] = "Disabled"
    del _namespace(objects)["metadata"]["labels"]["pod-security.kubernetes.io/enforce-version"]
    del _supervisor(objects)["metadata"]["labels"]["openshell.ai/component"]
    admission.inspect_worker(_SANDBOX_ID)


def _each_container(path: tuple[Any, ...], *values: object) -> list[Any]:
    return [
        pytest.param(_workload, (*container, *path), value, id=f"{name}-{'.'.join(path)}-{n}")
        for name, container in (
            ("agent", _AGENT),
            ("runtime-init", _RUNTIME_INIT),
            ("workspace-init", _WORKSPACE_INIT),
        )
        for n, value in enumerate(values)
    ]


def _case(
    target: Callable[[Objects], dict[str, Any]], name: str, path: tuple[Any, ...], *values: object
) -> list[Any]:
    return [pytest.param(target, path, value, id=f"{name}-{n}") for n, value in enumerate(values)]


_GPU = {"limits": {"cpu": "1", "memory": "2Gi", "nvidia.com/gpu": "1"}}
_CONTEXT = ("securityContext",)

_CONFINEMENT = [
    *_case(_workload, "token", ("spec", "automountServiceAccountToken"), _DELETE, True, None),
    *_case(_workload, "process-namespace", ("spec", "shareProcessNamespace"), _DELETE, True),
    *_case(_workload, "restart", ("spec", "restartPolicy"), _DELETE, "Always", "OnFailure"),
    *_case(_workload, "pod-user", ("spec", *_CONTEXT, "runAsUser"), _DELETE, 0, 1000, "10001"),
    *_case(_workload, "pod-group", ("spec", *_CONTEXT, "runAsGroup"), _DELETE, 0, True),
    *_case(_workload, "pod-fs-group", ("spec", *_CONTEXT, "fsGroup"), _DELETE, 0),
    *_case(_workload, "pod-non-root", ("spec", *_CONTEXT, "runAsNonRoot"), _DELETE, False),
    *_case(
        _workload,
        "pod-seccomp",
        ("spec", *_CONTEXT, "seccompProfile"),
        _DELETE,
        {"type": "Unconfined"},
        {"type": "Localhost", "localhostProfile": "profile.json"},
        {},
    ),
    *_case(_workload, "pod-groups", ("spec", *_CONTEXT, "supplementalGroups"), [0], [10001, 0]),
    *_case(_workload, "pod-context", ("spec", "securityContext"), _DELETE, None),
    *_each_container((*_CONTEXT, "allowPrivilegeEscalation"), _DELETE, True, None),
    *_each_container((*_CONTEXT, "capabilities"), _DELETE, {}, {"drop": []}),
    *_each_container((*_CONTEXT, "capabilities", "drop"), _DELETE, ["NET_RAW"], "ALL"),
    *_each_container((*_CONTEXT, "capabilities", "add"), ["NET_ADMIN"], ["ALL"]),
    *_each_container((*_CONTEXT, "privileged"), True),
    *_each_container((*_CONTEXT, "runAsUser"), _DELETE, 0),
    *_each_container((*_CONTEXT, "runAsGroup"), _DELETE, 0),
    *_each_container((*_CONTEXT, "runAsNonRoot"), _DELETE, False),
    *_each_container((*_CONTEXT, "seccompProfile"), {"type": "Unconfined"}, {}),
    *_each_container(_CONTEXT, _DELETE, None),
    *_each_container((*_CONTEXT, "appArmorProfile"), {"type": "Unconfined"}),
    *_each_container((*_CONTEXT, "seLinuxOptions"), {"type": "spc_t"}),
    *_each_container((*_CONTEXT, "procMount"), "Unmasked"),
    *_each_container((*_CONTEXT, "windowsOptions"), {"hostProcess": True}),
    *_each_container((*_CONTEXT, "capabilities", "unlisted"), ["ALL"]),
    *_each_container(("ports",), [{"containerPort": 5500, "hostPort": 5500}]),
    *_each_container(("ports",), [{"containerPort": 5500, "hostIP": "0.0.0.0"}]),
    *_each_container(("envFrom",), [{"secretRef": {"name": "openshell-client-tls"}}]),
    *_each_container(
        ("env",),
        [{"name": "KEY", "valueFrom": {"secretKeyRef": {"name": "os-sandbox-other", "key": "k"}}}],
        [{"name": "KEY", "valueFrom": {"configMapKeyRef": {"name": "other", "key": "k"}}}],
    ),
    *_each_container(("restartPolicy",), "Always"),
    *[
        case
        for target, name in ((_workload, "workload"), (_supervisor, "supervisor"))
        for case in (
            *_case(
                target,
                name + "-apparmor",
                ("spec", *_CONTEXT, "appArmorProfile"),
                {"type": "Unconfined"},
            ),
            *_case(
                target, name + "-selinux", ("spec", *_CONTEXT, "seLinuxOptions"), {"type": "spc_t"}
            ),
            *_case(
                target,
                name + "-windows",
                ("spec", *_CONTEXT, "windowsOptions"),
                {"hostProcess": True},
            ),
            *_case(
                target,
                name + "-sysctls",
                ("spec", *_CONTEXT, "sysctls"),
                [{"name": "kernel.shm_rmid_forced", "value": "1"}],
                [
                    {"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"},
                    {"name": "kernel.msgmax", "value": "65536"},
                ],
                None,
            ),
            *_case(target, name + "-runtime-class", ("spec", "runtimeClassName"), "other"),
            *_case(
                target,
                name + "-annotation",
                ("metadata", "annotations"),
                {"container.apparmor.security.beta.kubernetes.io/agent": "unconfined"},
                "unconfined",
                None,
            ),
        )
    ],
    *_case(_supervisor, "supervisor-fs-group", ("spec", *_CONTEXT, "fsGroup"), _DELETE, 0),
    *_case(
        _supervisor,
        "supervisor-groups",
        ("spec", *_CONTEXT, "supplementalGroups"),
        [0],
        [10001, 0],
    ),
    *_case(_supervisor, "supervisor-pod-user", ("spec", *_CONTEXT, "runAsUser"), 0, "10001"),
    *_case(_supervisor, "supervisor-pod-group", ("spec", *_CONTEXT, "runAsGroup"), 0),
    *_case(
        _supervisor,
        "supervisor-context",
        (*_SUPERVISOR, *_CONTEXT, "seLinuxOptions"),
        {"type": "spc_t"},
    ),
    *_case(
        _supervisor,
        "supervisor-environment",
        (*_SUPERVISOR, "envFrom"),
        [{"secretRef": {"name": "os-sandbox-other"}}],
    ),
    *_case(
        _supervisor,
        "supervisor-secret",
        ("spec", "volumes", 0, "secret"),
        {"secretName": "os-sandbox-0698f582-bdd8-4ddc-969c-96c0e749a85e-f8c054"},
        {"secretName": "os-supervisor-11111111-bdd8-4ddc-969c-96c0e749a85e-f8c054"},
        {"secretName": None},
        {},
    ),
    *_each_container(("volumeDevices",), [{"name": "workspace", "devicePath": "/dev/xvda"}]),
    *_case(
        _supervisor, "supervisor-token", ("spec", "automountServiceAccountToken"), _DELETE, True
    ),
    *_case(
        _supervisor,
        "supervisor-seccomp",
        ("spec", *_CONTEXT, "seccompProfile"),
        _DELETE,
        {"type": "Unconfined"},
    ),
    *_case(
        _supervisor,
        "supervisor-escalation",
        (*_SUPERVISOR, *_CONTEXT, "allowPrivilegeEscalation"),
        _DELETE,
        True,
    ),
    *_case(
        _supervisor,
        "supervisor-drop",
        (*_SUPERVISOR, *_CONTEXT, "capabilities", "drop"),
        _DELETE,
        [],
    ),
    *_case(
        _supervisor,
        "supervisor-add",
        (*_SUPERVISOR, *_CONTEXT, "capabilities", "add"),
        ["NET_ADMIN"],
    ),
    *_case(_supervisor, "supervisor-user", (*_SUPERVISOR, *_CONTEXT, "runAsUser"), _DELETE, 0),
    *_case(_supervisor, "supervisor-resources", (*_SUPERVISOR, "resources"), _DELETE, _GPU),
    *_case(
        _supervisor,
        "supervisor-claims",
        (*_SUPERVISOR, "resources", "claims"),
        [{"name": "gpu"}],
    ),
    *_case(_supervisor, "supervisor-pod-claims", ("spec", "resourceClaims"), [{"name": "gpu"}]),
    *_case(_supervisor, "supervisor-pod-resources", ("spec", "resources"), _GPU),
    *_case(
        _supervisor,
        "supervisor-volume",
        ("spec", "volumes", 2),
        {"name": "run", "hostPath": {"path": "/"}},
        {"name": "run", "configMap": {"name": "other"}},
        {"name": "run"},
        {"name": "run", "emptyDir": {}, "hostPath": {"path": "/"}},
    ),
    *_case(
        _supervisor,
        "supervisor-token-source",
        ("spec", "volumes", 1, "projected", "sources"),
        _DELETE,
        [],
        [{"serviceAccountToken": {"path": "token"}}],
        [{"serviceAccountToken": {"audience": "https://kubernetes.default.svc"}}],
        [{"configMap": {"name": "other"}}],
        [{"serviceAccountToken": {"audience": "openshell-gateway"}, "secret": {"name": "x"}}],
        [None],
    ),
]


@pytest.mark.parametrize(("target", "path", "value"), _CONFINEMENT)
def test_unconfined_or_unobserved_pods_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError, match="confinement does not match admission"):
        admission.inspect_worker(_SANDBOX_ID)


_TAG = "registry.test/heartwood/worker:latest"
_OTHER = "registry.test/heartwood/worker@sha256:" + "9" * 64


@pytest.mark.parametrize(
    ("target", "path", "value"),
    [
        *_each_container(("image",), _DELETE, _TAG, _OTHER, _IMAGE, "@" + _IMAGE, None),
        *_case(_workload, "agent-running", ("status", "containerStatuses", 0, "imageID"), _OTHER),
        *_case(_workload, "agent-status", ("status", "containerStatuses"), _DELETE, []),
        *_case(
            _workload,
            "init-running",
            ("status", "initContainerStatuses", 0, "imageID"),
            _DELETE,
            "ghcr.io/nvidia/openshell/sandbox@" + _IMAGE,
        ),
        *_case(_workload, "init-status", ("status", "initContainerStatuses", 1), _DELETE),
        *_case(_workload, "status", ("status",), _DELETE),
        *_case(_supervisor, "supervisor", (*_SUPERVISOR, "image"), _DELETE, _TAG, _OTHER),
        *_case(
            _supervisor,
            "supervisor-running",
            ("status", "containerStatuses", 0, "imageID"),
            _DELETE,
            _OTHER,
        ),
    ],
)
def test_images_other_than_the_pinned_digests_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError, match="image does not match admission"):
        admission.inspect_worker(_SANDBOX_ID)


def test_each_container_must_run_its_own_pinned_image(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    workload = _workload(objects)
    runtime = workload["spec"]["initContainers"][0]["image"]
    for container in (_AGENT, _WORKSPACE_INIT):
        changed = copy.deepcopy(workload)
        _change(changed, (*container, "image"), runtime)
        objects[f"pod/{admission.pod_name}"] = changed
        with pytest.raises(WorkerAttemptError, match="image does not match admission"):
            admission.inspect_worker(_SANDBOX_ID)
    objects[f"pod/{admission.pod_name}"] = workload
    _change(_supervisor(objects), (*_SUPERVISOR, "image"), runtime)
    with pytest.raises(WorkerAttemptError, match="image does not match admission"):
        admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    ("target", "path", "value"),
    [
        *_case(_workload, "limits", (*_AGENT, "resources", "limits"), _DELETE, {}, _GPU["limits"]),
        *_case(_workload, "cpu", (*_AGENT, "resources", "limits", "cpu"), "2", "500m", "1.5", 1),
        *_case(
            _workload,
            "memory",
            (*_AGENT, "resources", "limits", "memory"),
            "1Gi",
            "4Gi",
            "2G",
            "2e9",
            _DELETE,
        ),
        *_case(_workload, "requests", (*_AGENT, "resources", "requests"), _DELETE, {"cpu": "1"}),
        *_case(_workload, "agent-resources", (*_AGENT, "resources"), _DELETE, None),
        *_case(_workload, "agent-unlisted", (*_AGENT, "resources", "unlisted"), {}),
        *_case(_workload, "agent-claims", (*_AGENT, "resources", "claims"), [{"name": "gpu"}]),
        *_case(_workload, "init-resources", (*_RUNTIME_INIT, "resources"), _DELETE, _GPU),
        *_case(
            _workload,
            "init-requests",
            (*_WORKSPACE_INIT, "resources", "requests"),
            {"example.com/device": "1"},
        ),
        *_case(_workload, "pod-resources", ("spec", "resources"), _GPU, None),
        *_case(_workload, "pod-claims", ("spec", "resourceClaims"), [{"name": "gpu"}]),
    ],
)
def test_limits_other_than_the_manifest_and_extended_resources_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError, match="resource limits do not match admission"):
        admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    ("cpu", "memory", "limits"),
    [
        ("500m", "512Mi", {"cpu": "500m", "memory": "512Mi"}),
        ("2", "2048Mi", {"cpu": "2", "memory": "2Gi"}),
        ("1000m", "1Gi", {"cpu": "1", "memory": "1073741824"}),
        ("1", "1Gi", {"cpu": "1000m", "memory": "1048576Ki"}),
        ("64", "1024Gi", {"cpu": "64", "memory": "1Ti"}),
    ],
)
def test_limits_equal_to_the_manifest_are_accepted_in_any_reported_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cpu: str, memory: str, limits: dict[str, str]
) -> None:
    admission = KubernetesWorkerAdmission(_settings(tmp_path, cpu=cpu, memory=memory), _intent())
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(_workload(objects), (*_AGENT, "resources"), {"limits": limits, "requests": limits})
    admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    "limits",
    [
        {"cpu": "2", "memory": "2Gi"},
        {"cpu": "500m", "memory": "2Gi"},
        {"cpu": "1", "memory": "1Gi"},
        {"cpu": "1", "memory": "4Gi"},
        {"cpu": "1", "memory": "2147483649"},
        {"cpu": "1", "memory": "2G"},
        {"cpu": "1", "memory": "2Ti"},
        {"cpu": "1.0", "memory": "2Gi"},
        {"cpu": "1"},
        {"memory": "2Gi"},
        {},
    ],
)
def test_requests_and_limits_that_agree_with_each_other_but_not_the_manifest_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    limits: dict[str, str],
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(
        _workload(objects), (*_AGENT, "resources"), {"limits": limits, "requests": dict(limits)}
    )
    with pytest.raises(WorkerAttemptError, match="resource limits do not match admission"):
        admission.inspect_worker(_SANDBOX_ID)


def _volume(index: int, *path: str | int) -> tuple[Any, ...]:
    return ("spec", "volumes", index, *path)


_HOST_PATH = {"name": "host", "hostPath": {"path": "/"}}

_VOLUMES = [
    *_case(_workload, "host-path", _volume(8), _HOST_PATH),
    *_case(
        _workload,
        "replaced",
        _volume(2),
        {"name": "openshell-runtime", "hostPath": {"path": "/"}},
        {"name": "openshell-runtime", "emptyDir": {}, "hostPath": {"path": "/"}},
        {"name": "openshell-runtime"},
    ),
    *_case(_workload, "removed", _volume(0), _DELETE),
    *_case(_workload, "removed-runtime", _volume(3), _DELETE),
    *_case(_workload, "duplicate", _volume(8), {"name": "workspace", "emptyDir": {}}),
    *_case(_workload, "volumes", ("spec", "volumes"), _DELETE, []),
    *_case(
        _workload,
        "workspace",
        _volume(7),
        {"name": "workspace", "persistentVolumeClaim": {"claimName": "project-data"}},
        {"name": "workspace", "hostPath": {"path": "/"}},
        {"name": "workspace", "persistentVolumeClaim": None},
        {
            "name": "workspace",
            "persistentVolumeClaim": {"claimName": "workspace-hw--other"},
        },
    ),
    *_case(
        _workload,
        "bootstrap-secret",
        _volume(4, "secret", "secretName"),
        _DELETE,
        "openshell-client-tls",
        "os-sandbox-11111111-bdd8-4ddc-969c-96c0e749a85e-f8c054",
    ),
    *_case(
        _workload,
        "agent-bootstrap",
        (*_AGENT, "volumeMounts", 6),
        {"mountPath": "/bootstrap", "name": "openshell-sandbox-bootstrap", "readOnly": True},
    ),
    *_case(
        _workload,
        "workspace-init-bootstrap",
        (*_WORKSPACE_INIT, "volumeMounts", 0),
        {"mountPath": "/bootstrap", "name": "openshell-sandbox-bootstrap", "readOnly": True},
    ),
]


@pytest.mark.parametrize(("target", "path", "value"), _VOLUMES)
def test_volumes_outside_the_listed_set_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    if path == _volume(8):
        _workload(objects)["spec"]["volumes"].append(value)
    else:
        _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError, match="volume that admission does not list"):
        admission.inspect_worker(_SANDBOX_ID)


def test_the_workspace_volume_is_the_driver_claim_and_nothing_else(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    claim = {"claimName": "workspace-" + admission.pod_name}
    _change(_workload(objects), _volume(7, "persistentVolumeClaim"), {**claim, "readOnly": False})
    admission.inspect_worker(_SANDBOX_ID)
    for volume in (
        {"name": "workspace", "persistentVolumeClaim": claim, "hostPath": {"path": "/"}},
        {"name": "workspace", "persistentVolumeClaim": {**claim, "unlisted": True}},
    ):
        _change(_workload(objects), _volume(7), volume)
        with pytest.raises(WorkerAttemptError, match="volume that admission does not list"):
            admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    "kind",
    ["hostPath", "configMap", "projected", "csi", "ephemeral", "image", "nfs", "secret"],
)
def test_no_other_volume_type_is_admitted(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _workload(objects)["spec"]["volumes"].append({"name": "extra", kind: {}})
    with pytest.raises(WorkerAttemptError, match="volume that admission does not list"):
        admission.inspect_worker(_SANDBOX_ID)


_READ_ONLY_MOUNT = "projection mount is not read-only"
_UNOBSERVED_MOUNT = "projection mounts were not observed"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ((*_AGENT, "volumeMounts", 0, "readOnly"), False, _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "readOnly"), _DELETE, _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 1, "readOnly"), None, _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0), _DELETE, _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "mountPath"), "/data", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "mountPath"), "/inputs/data", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "name"), "skills", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "name"), "openshell-run", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "/inputs/hidden", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "/skills", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "/", _READ_ONLY_MOUNT),
        (
            (*_WORKSPACE_INIT, "volumeMounts", 0),
            {"mountPath": "/mnt/inputs", "name": "inputs"},
            _READ_ONLY_MOUNT,
        ),
        (_volume(0, "persistentVolumeClaim", "readOnly"), False, _READ_ONLY_MOUNT),
        (_volume(0, "persistentVolumeClaim", "readOnly"), _DELETE, _READ_ONLY_MOUNT),
        (_volume(1, "persistentVolumeClaim", "claimName"), "project-data", _READ_ONLY_MOUNT),
        (_volume(0), {"name": "inputs", "hostPath": {"path": "/"}}, _READ_ONLY_MOUNT),
        (_volume(0), {"name": "inputs", "emptyDir": {}}, _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "subPath"), "selected", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "subPathExpr"), "$(POD_NAME)", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 1, "mountPropagation"), "Bidirectional", _READ_ONLY_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "//inputs", _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "/run/../inputs", _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "/run/./x", _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "inputs", _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "/skills/", _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "mountPath"), "", _UNOBSERVED_MOUNT),
        ((*_WORKSPACE_INIT, "volumeMounts", 0, "mountPath"), "//mnt", _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "name"), ["openshell-run"], _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "mountPath"), None, _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 0, "mountPath"), _DELETE, _UNOBSERVED_MOUNT),
        ((*_AGENT, "volumeMounts", 5, "name"), "unlisted", _UNOBSERVED_MOUNT),
        ((*_RUNTIME_INIT, "volumeMounts", 1, "name"), _DELETE, _UNOBSERVED_MOUNT),
    ],
)
def test_projections_must_be_the_attempt_claims_mounted_read_only_exactly_once(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[Any, ...],
    value: object,
    message: str,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(_workload(objects), path, value)
    with pytest.raises(WorkerAttemptError, match=message):
        admission.inspect_worker(_SANDBOX_ID)


def test_claims_of_another_attempt_are_refused(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = KubernetesWorkerAdmission(admission.settings, _intent())
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    for index, mount in enumerate(("inputs", "skills")):
        changed = copy.deepcopy(objects)
        _change(
            _workload(changed),
            _volume(index, "persistentVolumeClaim", "claimName"),
            other.names[mount],
        )
        _serve(admission, monkeypatch, changed)
        with pytest.raises(WorkerAttemptError, match=_READ_ONLY_MOUNT):
            admission.inspect_worker(_SANDBOX_ID)


_CONTAINER = {"name": "debugger", "image": "registry.test/tools@" + _IMAGE}


@pytest.mark.parametrize(
    ("target", "path", "value", "message"),
    [
        (_workload, ("spec", "ephemeralContainers"), [_CONTAINER], "worker pod has unexpected"),
        (_workload, ("spec", "ephemeralContainers"), None, "worker pod has unexpected"),
        (_workload, ("spec", "containers", 1), _CONTAINER, "worker pod has unexpected"),
        (_workload, ("spec", "containers"), [], "worker pod has unexpected"),
        (_workload, (*_AGENT, "name"), "worker", "worker pod has unexpected"),
        (_workload, (*_AGENT, "name"), _DELETE, "worker pod has unexpected"),
        (_workload, ("spec", "initContainers", 2), _CONTAINER, "worker pod has unexpected"),
        (_workload, ("spec", "initContainers", 0), _DELETE, "worker pod has unexpected"),
        (_workload, ("spec", "initContainers"), _DELETE, "worker pod has unexpected"),
        (_workload, (*_RUNTIME_INIT, "name"), "workspace-init", "worker pod has unexpected"),
        (_supervisor, ("spec", "ephemeralContainers"), [_CONTAINER], "supervisor pod has"),
        (_supervisor, ("spec", "containers", 1), _CONTAINER, "supervisor pod has"),
        (_supervisor, ("spec", "containers"), _DELETE, "supervisor pod has"),
        (_supervisor, ("spec", "initContainers"), [_CONTAINER], "supervisor pod has"),
        (_supervisor, (*_SUPERVISOR, "name"), "agent", "supervisor pod has"),
    ],
)
def test_containers_other_than_the_driver_set_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
    message: str,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    group = target(objects)["spec"].get(path[1]) if len(path) == 3 else None
    if isinstance(group, list) and path[2] == len(group):
        group.append(value)
    else:
        _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError, match=message):
        admission.inspect_worker(_SANDBOX_ID)


_OWNER = ("metadata", "ownerReferences")
_FOREIGN = "native resource does not belong to this worker attempt"
_UNPAIRED = "supervisor pod does not belong to this sandbox"


@pytest.mark.parametrize(
    ("target", "path", "value", "message"),
    [
        (_workload, ("metadata", "labels"), _DELETE, _FOREIGN),
        (_workload, ("metadata", "labels", "heartwood.project"), str(uuid4()), _FOREIGN),
        (_workload, ("metadata", "labels", "heartwood.attempt"), _DELETE, _FOREIGN),
        (_workload, ("metadata", "labels", "heartwood.role"), "staging", _FOREIGN),
        (_workload, ("metadata", "labels", _PAIR), "another-sandbox", _FOREIGN),
        (_workload, ("metadata", "labels", _ROLE), "supervisor", _FOREIGN),
        (_workload, _OWNER, _DELETE, _FOREIGN),
        (_workload, _OWNER, [], _FOREIGN),
        (_workload, (*_OWNER, 0, "kind"), "ReplicaSet", _FOREIGN),
        (_workload, (*_OWNER, 0, "apiVersion"), "apps/v1", _FOREIGN),
        (_workload, (*_OWNER, 0, "apiVersion"), None, _FOREIGN),
        (_workload, (*_OWNER, 0, "apiVersion"), _DELETE, _FOREIGN),
        (_workload, (*_OWNER, 0, "name"), None, _FOREIGN),
        (_workload, (*_OWNER, 0, "controller"), _DELETE, _FOREIGN),
        (_workload, (*_OWNER, 0, "controller"), False, _FOREIGN),
        (_workload, (*_OWNER, 0, "name"), "hw--another", _FOREIGN),
        (_workload, (*_OWNER, 0, "uid"), "", _FOREIGN),
        (_workload, (*_OWNER, 0, "uid"), _DELETE, _FOREIGN),
        (_supervisor, ("metadata", "labels"), None, _UNPAIRED),
        (_supervisor, ("metadata", "labels", _PAIR), "another-sandbox", _UNPAIRED),
        (_supervisor, ("metadata", "labels", _ROLE), "workload", _UNPAIRED),
        (_supervisor, ("metadata", "labels", "openshell.ai/sandbox-id"), _DELETE, _UNPAIRED),
        (_supervisor, _OWNER, _DELETE, _UNPAIRED),
        (_supervisor, (*_OWNER, 0, "uid"), "9f915c5e-10d0-48d3-8453-483461cb9f84", _UNPAIRED),
        (_supervisor, (*_OWNER, 0, "name"), "hw--another", _UNPAIRED),
        (_supervisor, (*_OWNER, 0, "kind"), "Pod", _UNPAIRED),
    ],
)
def test_pods_of_another_attempt_or_sandbox_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
    message: str,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError, match=message):
        admission.inspect_worker(_SANDBOX_ID)


def test_a_second_owner_is_refused(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    for target, message in (
        (_workload, _FOREIGN),
        (_supervisor, _UNPAIRED),
        (_control, "network policies do not match admission"),
    ):
        objects = _objects(admission)
        _serve(admission, monkeypatch, objects)
        owners = target(objects)["metadata"]["ownerReferences"]
        owners.append({**owners[0], "uid": "9f915c5e-10d0-48d3-8453-483461cb9f84"})
        with pytest.raises(WorkerAttemptError, match=message):
            admission.inspect_worker(_SANDBOX_ID)


_ALLOW_ALL: list[dict[str, Any]] = [{}]


@pytest.mark.parametrize(
    ("target", "path", "value"),
    [
        (_control, ("spec",), _DELETE),
        (_control, ("spec", "egress"), _ALLOW_ALL),
        (_control, ("spec", "egress", 0, "to"), _DELETE),
        (_control, ("spec", "egress", 0, "ports", 0, "port"), 443),
        (_control, ("spec", "egress", 0, "to", 0, "podSelector", "matchLabels"), {}),
        (_control, ("spec", "ingress"), _ALLOW_ALL),
        (_control, ("spec", "podSelector"), {}),
        (_control, ("spec", "policyTypes"), ["Ingress"]),
        (_control, _OWNER, _DELETE),
        (_control, (*_OWNER, 0, "uid"), "9f915c5e-10d0-48d3-8453-483461cb9f84"),
        (_fence, ("spec",), _DELETE),
        (_fence, ("spec", "egress"), _ALLOW_ALL),
        (_fence, ("spec", "policyTypes"), ["Ingress"]),
        (_fence, ("spec", "podSelector"), {"matchLabels": {_ROLE: "supervisor"}}),
        (_fence, ("spec", "ingress"), _ALLOW_ALL),
        (_fence, ("spec", "ingress", 0, "from"), _DELETE),
    ],
)
def test_network_policies_other_than_the_driver_policies_are_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError, match="network policies do not match admission"):
        admission.inspect_worker(_SANDBOX_ID)


def _policy(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": _NAMESPACE},
        "spec": spec,
    }


_EGRESS_TO_ALL: dict[str, Any] = {"policyTypes": ["Egress"], "egress": [{}]}


@pytest.mark.parametrize(
    "spec",
    [
        {"podSelector": {}, **_EGRESS_TO_ALL},
        {"podSelector": {"matchLabels": {_ROLE: "workload"}}, **_EGRESS_TO_ALL},
        {"podSelector": {"matchLabels": {"heartwood.role": "worker"}}, **_EGRESS_TO_ALL},
        {
            "podSelector": {
                "matchExpressions": [{"key": _ROLE, "operator": "NotIn", "values": ["supervisor"]}]
            },
            **_EGRESS_TO_ALL,
        },
        {
            "podSelector": {"matchExpressions": [{"key": _PAIR, "operator": "Exists"}]},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"namespaceSelector": {}}]}],
        },
        # A selector that cannot be evaluated is never read as selecting nothing.
        {"podSelector": {"matchExpressions": [{"key": _ROLE, "operator": "Near"}]}},
        {"podSelector": {"matchLabels": []}},
        {"policyTypes": ["Egress"]},
    ],
)
def test_another_policy_that_allows_the_workload_pod_traffic_is_refused(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch, spec: dict[str, Any]
) -> None:
    objects = _objects(admission)
    objects["networkpolicy.networking.k8s.io/added"] = _policy("added", spec)
    _serve(admission, monkeypatch, objects)
    with pytest.raises(WorkerAttemptError):
        admission.inspect_worker(_SANDBOX_ID)
    assert admission.record is not None
    assert admission.record["network_policies"]["others"] == [{"name": "added", "spec": spec}]


def test_policies_that_only_deny_or_select_other_pods_are_accepted(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    others: dict[str, dict[str, Any]] = {
        "default-deny": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        "supervisors": {"podSelector": {"matchLabels": {_ROLE: "supervisor"}}, **_EGRESS_TO_ALL},
        "gateway": {
            "podSelector": {
                "matchExpressions": [{"key": "app", "operator": "In", "values": ["openshell"]}]
            },
            **_EGRESS_TO_ALL,
        },
        "unpaired": {
            "podSelector": {"matchExpressions": [{"key": _PAIR, "operator": "DoesNotExist"}]},
            **_EGRESS_TO_ALL,
        },
    }
    for name, spec in others.items():
        objects["networkpolicy.networking.k8s.io/" + name] = _policy(name, spec)
    _serve(admission, monkeypatch, objects)
    admission.inspect_worker(_SANDBOX_ID)
    assert admission.record is not None
    assert len(admission.record["network_policies"]["others"]) == 4


def test_a_policy_listing_from_another_namespace_is_refused(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    added = _policy("added", {"podSelector": {}, "policyTypes": ["Ingress"]})
    added["metadata"]["namespace"] = "another"
    objects["networkpolicy.networking.k8s.io/added"] = added
    _serve(admission, monkeypatch, objects)
    with pytest.raises(WorkerAttemptError, match="inspection was incomplete"):
        admission.inspect_worker(_SANDBOX_ID)


_LEVEL = ("metadata", "labels", "pod-security.kubernetes.io/enforce")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (_LEVEL, _DELETE),
        (_LEVEL, "baseline"),
        (_LEVEL, "privileged"),
        (("metadata", "labels", "pod-security.kubernetes.io/enforce-version"), "v1.0"),
        (("metadata", "labels", "pod-security.kubernetes.io/enforce-version"), None),
        (("metadata", "labels"), _DELETE),
        (("status", "phase"), "Terminating"),
        (("status",), _DELETE),
    ],
)
def test_namespace_without_the_restricted_pod_security_level_is_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(_namespace(objects), path, value)
    with pytest.raises(WorkerAttemptError, match="restricted Pod Security level"):
        admission.inspect_worker(_SANDBOX_ID)


def test_a_warning_or_audit_level_does_not_enforce(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    labels = _namespace(objects)["metadata"]["labels"]
    del labels["pod-security.kubernetes.io/enforce"]
    labels["pod-security.kubernetes.io/warn"] = "restricted"
    labels["pod-security.kubernetes.io/audit"] = "restricted"
    with pytest.raises(WorkerAttemptError, match="restricted Pod Security level"):
        admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    "prefix",
    [
        "pod/hw--",
        "pod/os-supervisor-",
        "networkpolicy.networking.k8s.io/os-control-",
        "networkpolicy.networking.k8s.io/openshell-",
        "namespace/",
    ],
)
@pytest.mark.parametrize(
    "response",
    [
        "missing",
        b"null",
        b"[]",
        b"{}",
        b"invalid",
        b"\xff",
        b"[" * 100_000,
        "kind",
        "name",
        "items",
    ],
)
def test_a_missing_or_unreadable_object_is_refused(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    response: str | bytes,
) -> None:
    objects = _objects(admission)
    (key,) = [name for name in objects if name.startswith(prefix)]
    replaced: dict[str, bytes] = {}
    if response == "missing":
        del objects[key]
    elif response == "kind":
        objects[key]["kind"] = "ConfigMap"
    elif response == "name":
        objects[key]["metadata"]["name"] = "another"
    elif response == "items":
        objects[key] = {"apiVersion": "v1", "kind": "List", "items": [objects[key]]}
    else:
        assert isinstance(response, bytes)
        replaced[key] = response
    calls = _serve(admission, monkeypatch, objects)
    served = admission.command

    def command(*arguments: str) -> bytes:
        output = served(*arguments)
        return replaced.get(f"{arguments[1]}/{arguments[2]}", output)

    monkeypatch.setattr(admission, "command", command)
    with pytest.raises(WorkerAttemptError):
        admission.inspect_worker(_SANDBOX_ID)
    assert all(call[0] == "get" for call in calls)


@pytest.mark.parametrize(
    "prefix",
    [
        "pod/hw--",
        "pod/os-supervisor-",
        "networkpolicy.networking.k8s.io/os-control-",
        "networkpolicy.networking.k8s.io/openshell-",
    ],
)
def test_an_object_reported_for_another_namespace_is_refused(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    objects = _objects(admission)
    for change in ("another", _DELETE):
        changed = copy.deepcopy(objects)
        _change(_named(changed, prefix), ("metadata", "namespace"), change)
        _serve(admission, monkeypatch, changed)
        with pytest.raises(WorkerAttemptError, match="inspection was incomplete"):
            admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    ("target", "path", "value"),
    [
        (_workload, ("spec",), None),
        (_workload, ("spec", "containers"), "agent"),
        (_workload, ("spec", "containers"), [None]),
        (_workload, ("spec", "initContainers"), {}),
        (_workload, ("spec", "volumes"), [None]),
        (_workload, ("spec", "volumes"), "inputs"),
        (_workload, (*_AGENT, "volumeMounts"), [None]),
        (_workload, (*_AGENT, "volumeMounts"), "inputs"),
        (_workload, ("status", "containerStatuses"), [None]),
        (_workload, ("status", "containerStatuses", 0, "name"), ["agent"]),
        (_workload, ("status", "initContainerStatuses", 0, "name"), {"name": "agent"}),
        (_workload, (*_AGENT, "ports"), [None]),
        (_workload, (*_AGENT, "env"), "SESSION_API_KEY"),
        (_workload, (*_AGENT, "name"), ["agent"]),
        (_supervisor, ("status", "containerStatuses", 0, "name"), ["supervisor"]),
        (_workload, ("metadata",), None),
        (_supervisor, ("spec",), None),
        (_supervisor, ("spec", "containers"), [None]),
        (_supervisor, ("spec", "volumes"), [None]),
        (_supervisor, ("status", "containerStatuses"), "running"),
    ],
)
def test_malformed_objects_are_refused_as_admission_failures(
    admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[Objects], dict[str, Any]],
    path: tuple[Any, ...],
    value: object,
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    _change(target(objects), path, value)
    with pytest.raises(WorkerAttemptError):
        admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    "sandbox_id", ["", "-o=yaml", "a/b", "A", "a b", "a" * 49, "--all-namespaces", "a.b"]
)
def test_sandbox_identities_that_cannot_name_objects_reach_no_tool(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch, sandbox_id: str
) -> None:
    calls = _serve(admission, monkeypatch, _objects(admission))
    with pytest.raises(WorkerAttemptError, match="cannot name Kubernetes objects"):
        admission.inspect_worker(sandbox_id)
    assert calls == []


def test_admission_requires_the_retained_image(tmp_path: Path) -> None:
    with pytest.raises(WorkerAttemptError, match="retained worker intent"):
        KubernetesWorkerAdmission(_settings(tmp_path, image_digest="sha256:" + "9" * 64), _intent())


def test_evidence_record_keeps_inspected_values_without_environment_or_labels(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    _serve(admission, monkeypatch, objects)
    record = admission_record(admission.observe(_SANDBOX_ID))
    text = json.dumps(record, sort_keys=True)
    assert _ATTEMPT_KEY not in text
    assert "SESSION_API_KEY" not in text
    # Object names carry the attempt identifier; no label, and so no project, is recorded.
    assert "heartwood.project" not in text
    assert admission.labels["heartwood.project"] not in text
    workload = record["workload_pod"]
    assert workload["spec"]["hostNetwork"] == "<absent>"
    assert workload["spec"]["hostUsers"] == "<absent>"
    assert workload["spec"]["automountServiceAccountToken"] is False
    assert workload["spec"]["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert [item["name"] for item in workload["containers"]] == ["agent"]
    assert workload["containers"][0]["resources"]["limits"] == {"cpu": "1", "memory": "2Gi"}
    assert workload["containers"][0]["volumeDevices"] == "<absent>"
    assert workload["containers"][0]["restartPolicy"] == "<absent>"
    assert workload["containers"][0]["ports"] == "<absent>"
    assert workload["spec"]["runtimeClassName"] == "<absent>"
    assert workload["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert len(workload["initContainers"]) == 2
    assert workload["ephemeralContainers"] == []
    assert workload["image_ids"]["agent"].endswith("@" + _IMAGE)
    assert workload["owner_references"][0]["uid"] == _SANDBOX_UID
    assert record["supervisor_pod"]["image_ids"]["supervisor"].endswith("@" + _SUPERVISOR_IMAGE)
    assert record["supervisor_pod"]["owner_references"][0]["uid"] == _SANDBOX_UID
    assert record["network_policies"]["workload_policy"]["name"] == "openshell-sandbox-workloads"
    assert record["network_policies"]["control_policy"]["spec"]["policyTypes"] == [
        "Ingress",
        "Egress",
    ]
    assert record["namespace"] == {
        "phase": "Active",
        "pod_security": {
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "latest",
        },
    }


def test_evidence_record_shows_the_values_of_a_refused_worker(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _objects(admission)
    workload = _workload(objects)
    workload["spec"]["hostNetwork"] = True
    workload["spec"]["ephemeralContainers"] = [
        {**_CONTAINER, "env": [{"name": "SESSION_API_KEY", "value": _ATTEMPT_KEY}]}
    ]
    workload["spec"]["volumes"].append(_HOST_PATH)
    workload["spec"]["initContainers"] = "malformed"
    del _control(objects)["spec"]
    del _namespace(objects)["metadata"]["labels"]
    _serve(admission, monkeypatch, objects)
    with pytest.raises(WorkerAttemptError):
        admission.inspect_worker(_SANDBOX_ID)
    record = admission_record(admission.observe(_SANDBOX_ID))
    assert _ATTEMPT_KEY not in json.dumps(record)
    assert record["workload_pod"]["spec"]["hostNetwork"] is True
    assert record["workload_pod"]["spec"]["volumes"][-1] == _HOST_PATH
    assert record["workload_pod"]["ephemeralContainers"][0]["name"] == "debugger"
    assert record["workload_pod"]["initContainers"] == []
    assert record["network_policies"]["control_policy"]["spec"] == "<absent>"
    assert record["namespace"]["pod_security"] == {}


_FAKE_KUBECTL = """#!{python} -I
import json, os, sys
from pathlib import Path
root = Path(__file__).parent
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}))
    log.write("\\n")
arguments = sys.argv[1:]
position = arguments.index("get")
if arguments[position + 2] == "--output=json":
    found = sorted((root / "objects").glob(arguments[position + 1] + "--*.json"))
    items = [json.loads(path.read_text()) for path in found]
    sys.stdout.write(json.dumps({{"kind": "List", "items": items}}))
    sys.exit(0)
source = root / "objects" / (arguments[position + 1] + "--" + arguments[position + 2] + ".json")
if not source.exists():
    sys.stderr.write("Error from server (NotFound)\\n")
    sys.exit(1)
sys.stdout.write(source.read_text())
"""


def _install(admission: KubernetesWorkerAdmission, objects: Objects) -> Path:
    """Install an executable that answers as `kubectl get` does and records each call."""
    root = admission.settings.kubectl_cli.parent
    (root / "objects").mkdir()
    for key, value in objects.items():
        (root / "objects" / (key.replace("/", "--") + ".json")).write_text(json.dumps(value))
    admission.settings.kubectl_cli.write_text(_FAKE_KUBECTL.format(python=sys.executable))
    admission.settings.kubectl_cli.chmod(0o700)
    return root


def test_admission_runs_the_explicit_tool_with_the_manifest_cluster_and_namespace(
    admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _install(admission, _objects(admission))
    monkeypatch.setenv("KUBECONFIG", "/ambient/kubeconfig")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    admission.inspect_worker(_SANDBOX_ID)
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 6
    for call in calls:
        argv, home = call["argv"], call["cwd"]
        assert argv[:4] == ["--kubeconfig", str(root / "kubeconfig"), "--namespace", _NAMESPACE]
        assert argv[4] == "--cache-dir"
        assert argv[6:8] == ["--request-timeout=30s", "get"]
        assert argv[-1] == "--output=json"
        assert set(call["env"]) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == {"PATH", "HOME"}
        assert call["env"]["PATH"] == os.defpath
        assert Path(call["env"]["HOME"]).name.startswith("worker-kubectl-")
        assert Path(home).name == Path(call["env"]["HOME"]).name
        assert not Path(call["env"]["HOME"]).exists()
    assert [call["argv"][8:10] for call in calls] == [
        ["pod", admission.pod_name],
        ["pod", "os-supervisor-" + _SANDBOX_ID],
        ["networkpolicy.networking.k8s.io", "os-control-" + _SANDBOX_ID],
        ["networkpolicy.networking.k8s.io", "openshell-sandbox-workloads"],
        ["networkpolicy.networking.k8s.io", "--output=json"],
        ["namespace", _NAMESPACE],
    ]


def test_a_failing_missing_or_oversized_tool_answer_is_refused(
    admission: KubernetesWorkerAdmission,
) -> None:
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        admission.inspect_worker(_SANDBOX_ID)
    objects = _objects(admission)
    del objects[f"pod/os-supervisor-{_SANDBOX_ID}"]
    _workload(objects)["metadata"]["annotations"]["padding"] = "x" * (1024 * 1024)
    root = _install(admission, objects)
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        admission.inspect_worker(_SANDBOX_ID)
    assert len((root / "calls.jsonl").read_text().splitlines()) == 1
    del _workload(objects)["metadata"]["annotations"]["padding"]
    (root / "objects" / f"pod--{admission.pod_name}.json").write_text(
        json.dumps(_workload(objects))
    )
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        admission.inspect_worker(_SANDBOX_ID)
    assert len((root / "calls.jsonl").read_text().splitlines()) == 3


_CLAIM_UID = "6f1b8c2e-4d3a-4b5c-9e8f-0a1b2c3d4e5f"
_CONTENT = "e" * 64


def _project_data(**changes: object) -> dict[str, object]:
    return {
        "claim_name": "hw-project-data-eeeeeeeeeeeeeeee",
        "claim_uid": _CLAIM_UID,
        "project": "demo",
        "content_sha256": _CONTENT,
        "mount_path": "/data",
        **changes,
    }


@pytest.fixture
def data_admission(tmp_path: Path) -> KubernetesWorkerAdmission:
    return KubernetesWorkerAdmission(_settings(tmp_path, project_data=_project_data()), _intent())


def _with_project_data(admission: KubernetesWorkerAdmission) -> Objects:
    """The sandbox with the declared claim attached, as the driver attaches it."""
    objects = _objects(admission)
    spec = _workload(objects)["spec"]
    spec["volumes"].insert(
        2,
        {
            "name": "project-data",
            "persistentVolumeClaim": {
                "claimName": "hw-project-data-eeeeeeeeeeeeeeee",
                "readOnly": True,
            },
        },
    )
    spec["containers"][0]["volumeMounts"].insert(
        2, {"mountPath": "/data", "name": "project-data", "readOnly": True}
    )
    objects["persistentvolumeclaim/hw-project-data-eeeeeeeeeeeeeeee"] = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "annotations": {"heartwood.platform/content-sha256": _CONTENT},
            "labels": {
                "heartwood.platform/project": "demo",
                "heartwood.platform/role": "project-data",
                "heartwood.platform/session": "0123456789abcdef",
                "openshell.ai/sandbox-attachable": "true",
                "openshell.ai/sandbox-attachable-workspace": "hw",
            },
            "name": "hw-project-data-eeeeeeeeeeeeeeee",
            "namespace": _NAMESPACE,
            "uid": _CLAIM_UID,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "heartwood-project-data",
            "volumeName": "pvc-1",
        },
        "status": {"phase": "Bound"},
    }
    return objects


def test_a_declared_project_data_volume_is_admitted_and_recorded(
    data_admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _with_project_data(data_admission)
    _serve(data_admission, monkeypatch, objects)
    data_admission.inspect_worker(_SANDBOX_ID)
    assert data_admission.record is not None
    assert data_admission.record["project_data"] == {
        "claim_name": "hw-project-data-eeeeeeeeeeeeeeee",
        "claim_uid": _CLAIM_UID,
        "project": "demo",
        "content_sha256": _CONTENT,
        "access_modes": ["ReadWriteOnce"],
    }


def test_without_a_declaration_a_project_data_volume_is_refused(
    admission: KubernetesWorkerAdmission,
    data_admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects = _with_project_data(data_admission)
    # The same sandbox seen by an attempt whose manifest declares no project data.
    for name in ("pod/", "networkpolicy", "namespace/"):
        for key in [key for key in objects if key.startswith(name)]:
            objects[key.replace(data_admission.pod_name, admission.pod_name)] = objects.pop(key)
    workload = _workload(objects)
    workload["metadata"]["labels"].update(admission.labels)
    workload["metadata"]["name"] = admission.pod_name
    workload["metadata"]["ownerReferences"][0]["name"] = admission.pod_name
    for volume in workload["spec"]["volumes"]:
        claim = volume.get("persistentVolumeClaim", {})
        for mount in ("inputs", "skills"):
            if volume["name"] == mount:
                claim["claimName"] = admission.names[mount]
        if volume["name"] == "workspace":
            claim["claimName"] = "workspace-" + admission.pod_name
    for item in objects.values():
        for owner in item["metadata"].get("ownerReferences", []):
            owner["name"] = admission.pod_name
    _serve(admission, monkeypatch, objects)
    with pytest.raises(WorkerAttemptError, match="volume that admission does not list"):
        admission.inspect_worker(_SANDBOX_ID)


_CLAIM_KEY = "persistentvolumeclaim/hw-project-data-eeeeeeeeeeeeeeee"


@pytest.mark.parametrize(
    ("target", "path", "value", "refusal"),
    [
        # The claim belongs to another project.
        (
            _CLAIM_KEY,
            ("metadata", "labels", "heartwood.platform/project"),
            "other",
            "another project",
        ),
        (
            _CLAIM_KEY,
            ("metadata", "labels", "heartwood.platform/project"),
            _DELETE,
            "another project",
        ),
        # Another claim under the declared name, other content or another workspace.
        (
            _CLAIM_KEY,
            ("metadata", "uid"),
            "6f1b8c2e-0000-4b5c-9e8f-0a1b2c3d4e5f",
            "its declaration",
        ),
        (
            _CLAIM_KEY,
            ("metadata", "annotations", "heartwood.platform/content-sha256"),
            "f" * 64,
            "its declaration",
        ),
        (
            _CLAIM_KEY,
            ("metadata", "labels", "openshell.ai/sandbox-attachable-workspace"),
            "other",
            "its declaration",
        ),
        (
            _CLAIM_KEY,
            ("spec", "dataSource"),
            {"kind": "PersistentVolumeClaim", "name": "x"},
            "its declaration",
        ),
        # The volume or its mount is writable.
        (
            "pod/hw--",
            ("spec", "volumes", 2, "persistentVolumeClaim", "readOnly"),
            False,
            "read-only at its declared path",
        ),
        (
            "pod/hw--",
            ("spec", "volumes", 2, "persistentVolumeClaim", "readOnly"),
            _DELETE,
            "read-only at its declared path",
        ),
        (
            "pod/hw--",
            ("spec", "containers", 0, "volumeMounts", 2, "readOnly"),
            False,
            "read-only at its declared path",
        ),
        (
            "pod/hw--",
            ("spec", "containers", 0, "volumeMounts", 2, "readOnly"),
            _DELETE,
            "read-only at its declared path",
        ),
        # The volume is mounted at an undeclared path, or names another claim.
        (
            "pod/hw--",
            ("spec", "containers", 0, "volumeMounts", 2, "mountPath"),
            "/project",
            "read-only at its declared path",
        ),
        (
            "pod/hw--",
            ("spec", "containers", 0, "volumeMounts", 2, "mountPath"),
            "/data/sub",
            "read-only at its declared path",
        ),
        (
            "pod/hw--",
            ("spec", "volumes", 2, "persistentVolumeClaim", "claimName"),
            "hw-project-data-other",
            "read-only at its declared path",
        ),
    ],
)
def test_project_data_out_of_its_declaration_is_refused(
    data_admission: KubernetesWorkerAdmission,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    path: tuple[Any, ...],
    value: object,
    refusal: str,
) -> None:
    objects = _with_project_data(data_admission)
    _change(_named(objects, target), path, value)
    _serve(data_admission, monkeypatch, objects)
    with pytest.raises(WorkerAttemptError, match=refusal):
        data_admission.inspect_worker(_SANDBOX_ID)
    assert data_admission.record is not None


def test_a_second_mount_of_the_project_data_volume_is_refused(
    data_admission: KubernetesWorkerAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = _with_project_data(data_admission)
    mounts = _workload(objects)["spec"]["containers"][0]["volumeMounts"]
    mounts.append({"mountPath": "/elsewhere", "name": "project-data", "readOnly": True})
    _serve(data_admission, monkeypatch, objects)
    with pytest.raises(WorkerAttemptError, match="read-only at its declared path"):
        data_admission.inspect_worker(_SANDBOX_ID)


@pytest.mark.parametrize(
    "path",
    [
        "data",
        "/",
        "/data/../etc",
        "/inputs",
        "/inputs/data",
        "/workspace/data",
        "/sandbox",
        "/opt/data",
    ],
)
def test_project_data_is_mounted_apart_from_the_workers_own_paths(
    tmp_path: Path, path: str
) -> None:
    with pytest.raises(ValueError, match="apart from the worker's own"):
        _settings(tmp_path, project_data=_project_data(mount_path=path))
    assert _settings(tmp_path, project_data=_project_data(mount_path="/project/data")).project_data


def test_the_driver_configuration_attaches_the_project_data_read_only(tmp_path: Path) -> None:
    from heartwood.gateway.worker_kubernetes import KubernetesWorkerVolumes

    volumes = KubernetesWorkerVolumes(_settings(tmp_path, project_data=_project_data()), _intent())
    config = volumes.driver_config()
    assert config["volumes"][-1] == {
        "name": "project-data",
        "persistent_volume_claim": {
            "claim_name": "hw-project-data-eeeeeeeeeeeeeeee",
            "read_only": True,
        },
    }
    assert config["containers"]["agent"]["volume_mounts"][-1] == {
        "name": "project-data",
        "mount_path": "/data",
        "read_only": True,
    }
