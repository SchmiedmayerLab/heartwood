# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Admission of a Kubernetes worker by reading its pods, policies and namespace with `kubectl`."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from heartwood.core_adapter.worker_attempts import WorkerAttemptError, WorkerAttemptIntent
from heartwood.gateway.worker_deployment import KubernetesWorkerDeployment
from heartwood.gateway.worker_volumes import _native_limits

_WORKER_ID = 10001
_PAIR = "openshell.ai/boundary-pair"
_ROLE = "openshell.ai/boundary-role"
_SANDBOX = "openshell.ai/sandbox-id"
_POD_SECURITY = "pod-security.kubernetes.io/enforce"
# The policy the driver keeps in the namespace: workload pods accept their supervisor's
# control connection and have no egress of their own.
_FENCE_POLICY = "openshell-sandbox-workloads"
_CONTROL_PORT = [{"port": 5500, "protocol": "TCP"}]
_FENCE_SPEC = {
    "ingress": [
        {"from": [{"podSelector": {"matchLabels": {_ROLE: "supervisor"}}}], "ports": _CONTROL_PORT}
    ],
    "podSelector": {"matchLabels": {_ROLE: "workload"}},
    "policyTypes": ["Ingress", "Egress"],
}
_AGENT = "agent"
_RUNTIME_INIT = "openshell-sandbox-bootstrap"
_WORKSPACE_INIT = "workspace-init"
_SUPERVISOR = "supervisor"
# Volumes the driver adds to every workload pod, by name and type. The attempt's two
# projections and the driver's workspace claim are the only other volumes admitted.
_DRIVER_VOLUMES = {
    "openshell-runtime": "emptyDir",
    "openshell-runtime-state": "emptyDir",
    "openshell-run": "emptyDir",
    "openshell-pod-identity": "downwardAPI",
    "openshell-sandbox-bootstrap": "secret",
}
_SUPERVISOR_VOLUME_TYPES = frozenset({"emptyDir", "secret", "projected"})
# The supervisor authenticates to the sandbox gateway only; a token for another audience
# could be presented to the cluster's own API.
_SUPERVISOR_TOKEN_AUDIENCE = "openshell-gateway"
_SANDBOX_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
_CPU = re.compile(r"([0-9]+)(m?)")
_MEMORY = re.compile(r"([0-9]+)(Ki|Mi|Gi)?")
_MEMORY_UNITS = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}

# What the evidence record keeps of each pod and container: the inspected values, without
# environment, commands, labels or annotations.
_RECORDED_POD_FIELDS = (
    "hostNetwork",
    "hostPID",
    "hostIPC",
    "hostUsers",
    "shareProcessNamespace",
    "automountServiceAccountToken",
    "securityContext",
    "resources",
    "resourceClaims",
    "volumes",
)
_RECORDED_CONTAINER_FIELDS = (
    "name",
    "image",
    "securityContext",
    "resources",
    "volumeMounts",
    "volumeDevices",
)
_ABSENT_RECORD = "<absent>"


class _Absent:
    """A field the API response does not contain, distinct from every reported value."""


_ABSENT = _Absent()


def _field(value: object, *path: str) -> object:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _ABSENT
        value = value[key]
    return value


def _default(value: object, default: bool) -> bool:
    """Accept a field the API server omits at its default value, or that default itself.

    Only `hostNetwork`, `hostPID`, `hostIPC`, `privileged` and `hostUsers` are read this way.
    """
    return value is _ABSENT or value is default


def _none(value: object) -> bool:
    """Accept a list without entries, which the API server omits from its response."""
    return value is _ABSENT or value == []


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    """Return a list field's objects; the API server omits a list without entries."""
    reported = _field(value, name)
    if reported is _ABSENT:
        return []
    if not isinstance(reported, list) or any(not isinstance(item, dict) for item in reported):
        raise WorkerAttemptError("native resource inspection was incomplete")
    return reported


def _require(accepted: bool, message: str) -> None:
    if not accepted:
        raise WorkerAttemptError(message)


def _pinned(reference: object, digest: str) -> bool:
    """Accept only an image reference by digest, never a tag that could move."""
    return (
        isinstance(reference, str)
        and reference.endswith("@" + digest)
        and len(reference) > len(digest) + 1
    )


def _confined(container: dict[str, Any]) -> bool:
    context = _field(container, "securityContext")
    drop = _field(context, "capabilities", "drop")
    # A container profile replaces the pod's, so only the same profile is accepted.
    profile = _field(context, "seccompProfile")
    return (
        _field(context, "allowPrivilegeEscalation") is False
        and _default(_field(context, "privileged"), False)
        and isinstance(drop, list)
        and "ALL" in drop
        and _none(_field(context, "capabilities", "add"))
        and _is_worker_id(_field(context, "runAsUser"))
        and _is_worker_id(_field(context, "runAsGroup"))
        and _field(context, "runAsNonRoot") is True
        and (profile is _ABSENT or profile == {"type": "RuntimeDefault"})
        and _none(_field(container, "volumeDevices"))
    )


def _is_worker_id(value: object) -> bool:
    return type(value) is int and value == _WORKER_ID


def _host_namespaces_private(spec: object) -> bool:
    return (
        _default(_field(spec, "hostNetwork"), False)
        and _default(_field(spec, "hostPID"), False)
        and _default(_field(spec, "hostIPC"), False)
        # The host user namespace, as on the Docker reference deployment.
        and _default(_field(spec, "hostUsers"), True)
    )


def _cpu_and_memory_only(resources: object) -> bool:
    """Refuse extended resources such as GPUs, and devices claimed by name."""
    if not isinstance(resources, dict) or set(resources) - {"limits", "requests", "claims"}:
        return False
    return _none(resources.get("claims", _ABSENT)) and all(
        isinstance(resources.get(kind, {}), dict)
        and set(resources.get(kind, {})) <= {"cpu", "memory"}
        for kind in ("limits", "requests")
    )


def _observed_limits(limits: object) -> tuple[int, int] | None:
    """Return CPU and memory limits in the units Docker reports, or nothing when unreadable."""
    cpu, memory = _field(limits, "cpu"), _field(limits, "memory")
    if not isinstance(cpu, str) or not isinstance(memory, str):
        return None
    cpu_match, memory_match = _CPU.fullmatch(cpu), _MEMORY.fullmatch(memory)
    if cpu_match is None or memory_match is None:
        return None
    return (
        int(cpu_match[1]) * (1_000_000 if cpu_match[2] else 1_000_000_000),
        int(memory_match[1]) * _MEMORY_UNITS[memory_match[2]],
    )


def _sandbox_owner(metadata: object, *, controller: bool = False) -> tuple[str, str] | None:
    """Return the name and UID of the one owning Sandbox resource, or nothing without one.

    With `controller`, the Sandbox resource must also be the object's controller.
    """
    owners = _field(metadata, "ownerReferences")
    if not isinstance(owners, list) or len(owners) != 1:
        return None
    owner = owners[0]
    version, name, uid = (_field(owner, key) for key in ("apiVersion", "name", "uid"))
    if (
        _field(owner, "kind") != "Sandbox"
        or not isinstance(version, str)
        or not version.startswith("agents.x-k8s.io/")
        or not isinstance(name, str)
        or not isinstance(uid, str)
        or not uid
        or (controller and _field(owner, "controller") is not True)
    ):
        return None
    return name, uid


def _supervisor_volume(volume: dict[str, Any]) -> bool:
    """Accept the driver's volume types, and a projected token for the sandbox gateway only."""
    kinds = set(volume) - {"name"}
    if len(kinds) != 1 or not kinds <= _SUPERVISOR_VOLUME_TYPES:
        return False
    if "projected" not in volume:
        return True
    sources = _field(volume, "projected", "sources")
    return (
        isinstance(sources, list)
        and bool(sources)
        and all(
            isinstance(source, dict)
            and set(source) == {"serviceAccountToken"}
            and _field(source, "serviceAccountToken", "audience") == _SUPERVISOR_TOKEN_AUDIENCE
            for source in sources
        )
    )


def _kubectl(settings: KubernetesWorkerDeployment, *args: str) -> bytes:
    """Use the explicit cluster configuration without inherited Kubernetes or proxy settings."""
    try:
        with TemporaryDirectory(prefix="worker-kubectl-") as home:
            result = subprocess.run(
                [
                    str(settings.kubectl_cli),
                    "--kubeconfig",
                    str(settings.kubeconfig),
                    "--namespace",
                    settings.namespace,
                    "--cache-dir",
                    home,
                    "--request-timeout=30s",
                    *args,
                ],
                env={"PATH": os.defpath, "HOME": home},
                cwd=home,
                capture_output=True,
                timeout=60,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkerAttemptError("native Kubernetes operation requires reconciliation") from None
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise WorkerAttemptError("native Kubernetes operation requires reconciliation")
    return result.stdout


def _recorded(value: object, *path: str) -> list[dict[str, Any]]:
    """Return the objects of a list for the record; a rejected pod may report anything."""
    reported = _field(value, *path)
    return (
        [item for item in reported if isinstance(item, dict)] if isinstance(reported, list) else []
    )


def _pod_record(pod: object) -> dict[str, Any]:
    spec = _field(pod, "spec")
    spec = spec if isinstance(spec, dict) else {}
    return {
        "owner_references": [
            {key: owner.get(key, _ABSENT_RECORD) for key in ("kind", "name", "uid", "controller")}
            for owner in _recorded(pod, "metadata", "ownerReferences")
        ],
        "spec": {name: spec.get(name, _ABSENT_RECORD) for name in _RECORDED_POD_FIELDS},
        **{
            group: [
                {name: item.get(name, _ABSENT_RECORD) for name in _RECORDED_CONTAINER_FIELDS}
                for item in _recorded(spec, group)
            ]
            for group in ("containers", "initContainers", "ephemeralContainers")
        },
        "image_ids": {
            str(item.get("name")): item.get("imageID", _ABSENT_RECORD)
            for group in ("containerStatuses", "initContainerStatuses")
            for item in _recorded(pod, "status", group)
        },
    }


def admission_record(observed: dict[str, Any]) -> dict[str, Any]:
    """Keep what Kubernetes reports about a worker's confinement, for qualification evidence.

    The record has no environment, command, label or annotation, so it carries neither the
    attempt key nor project names.
    """
    namespace = observed.get("namespace")
    labels = _field(namespace, "metadata", "labels")
    return {
        "workload_pod": _pod_record(observed.get("workload_pod")),
        "supervisor_pod": _pod_record(observed.get("supervisor_pod")),
        "network_policies": {
            name: {
                "name": _reported(_field(observed.get(name), "metadata", "name")),
                "spec": _reported(_field(observed.get(name), "spec")),
            }
            for name in ("control_policy", "workload_policy")
        },
        "namespace": {
            "phase": _reported(_field(namespace, "status", "phase")),
            "pod_security": {
                name: value
                for name, value in (labels if isinstance(labels, dict) else {}).items()
                if name.startswith("pod-security.kubernetes.io/")
            },
        },
    }


def _reported(value: object) -> object:
    return _ABSENT_RECORD if value is _ABSENT else value


class KubernetesWorkerAdmission:
    """Read one attempt's pods, network policies and namespace, and decide on listed values."""

    def __init__(self, settings: KubernetesWorkerDeployment, intent: WorkerAttemptIntent) -> None:
        """Retain the identities every inspected object must carry."""
        if settings.image_digest != intent.image_digest:
            raise WorkerAttemptError("worker image does not match the retained worker intent")
        self.settings = settings
        self.labels = {
            "heartwood.project": str(intent.project_id),
            "heartwood.attempt": str(intent.attempt_id),
        }
        # The claims the attempt's inputs and Skills are staged into.
        self.names = {
            mount: f"hw-attempt-{intent.attempt_id.hex}-{mount}" for mount in ("inputs", "skills")
        }
        self.worker_name = f"hw-{intent.attempt_id.hex[:16]}"
        # The driver names the Sandbox resource and its workload pod after both.
        self.pod_name = f"{settings.workspace}--{self.worker_name}"

    def command(self, *args: str) -> bytes:
        """Use the explicit cluster configuration without inherited Kubernetes settings."""
        return _kubectl(self.settings, *args)

    def _get(self, resource: str, kind: str, name: str) -> dict[str, Any]:
        """Read one object; a missing, unreadable or differently named object is refused."""
        output = self.command("get", resource, name, "--output=json")
        try:
            value = json.loads(output)
        except (ValueError, UnicodeError):
            raise WorkerAttemptError("native resource inspection was incomplete") from None
        if (
            not isinstance(value, dict)
            or value.get("kind") != kind
            or _field(value, "metadata", "name") != name
            or (
                kind != "Namespace"
                and _field(value, "metadata", "namespace") != self.settings.namespace
            )
        ):
            raise WorkerAttemptError("native resource inspection was incomplete")
        return value

    def observe(self, sandbox_id: str) -> dict[str, Any]:
        """Read the objects admission decides on, as the Kubernetes API reports them."""
        if _SANDBOX_ID.fullmatch(sandbox_id) is None:
            raise WorkerAttemptError("native sandbox identity cannot name Kubernetes objects")
        policy = "networkpolicy.networking.k8s.io"
        return {
            "workload_pod": self._get("pod", "Pod", self.pod_name),
            "supervisor_pod": self._get("pod", "Pod", "os-supervisor-" + sandbox_id),
            "control_policy": self._get(policy, "NetworkPolicy", "os-control-" + sandbox_id),
            "workload_policy": self._get(policy, "NetworkPolicy", _FENCE_POLICY),
            "namespace": self._get("namespace", "Namespace", self.settings.namespace),
        }

    def inspect_worker(self, sandbox_id: str) -> None:
        """Observe the worker's pods, policies and namespace outside the worker, and decide."""
        observed = self.observe(sandbox_id)
        sandbox = self._inspect_workload(observed["workload_pod"], sandbox_id)
        self._inspect_supervisor(observed["supervisor_pod"], sandbox_id, sandbox)
        self._inspect_policies(observed, sandbox_id, sandbox)
        namespace = observed["namespace"]
        if (
            _field(namespace, "metadata", "labels", _POD_SECURITY) != "restricted"
            or _field(namespace, "status", "phase") != "Active"
        ):
            raise WorkerAttemptError(
                "native worker namespace does not enforce the restricted Pod Security level"
            )

    def _inspect_workload(self, pod: dict[str, Any], sandbox_id: str) -> tuple[str, str]:
        """Decide on the workload pod and return the name and UID of its Sandbox resource."""
        metadata, spec = _field(pod, "metadata"), _field(pod, "spec")
        labels = _field(metadata, "labels")
        expected = {
            **self.labels,
            "heartwood.role": "worker",
            _PAIR: sandbox_id,
            _ROLE: "workload",
        }
        sandbox = _sandbox_owner(metadata, controller=True)
        if (
            not isinstance(labels, dict)
            or any(labels.get(name) != value for name, value in expected.items())
            or sandbox is None
            or sandbox[0] != self.pod_name
        ):
            raise WorkerAttemptError("native resource does not belong to this worker attempt")
        containers = {group: _objects(spec, group) for group in ("containers", "initContainers")}
        if (
            [item.get("name") for item in containers["containers"]] != [_AGENT]
            or [item.get("name") for item in containers["initContainers"]]
            != [_RUNTIME_INIT, _WORKSPACE_INIT]
            # A container added later runs in the pod's namespaces outside the sandbox.
            or not _none(_field(spec, "ephemeralContainers"))
        ):
            raise WorkerAttemptError("native worker pod has unexpected containers")
        agent = containers["containers"][0]
        runtime_init, workspace_init = containers["initContainers"]
        everything = (agent, runtime_init, workspace_init)
        images = {
            _AGENT: self.settings.image_digest,
            _RUNTIME_INIT: self.settings.sandbox_image_digest,
            _WORKSPACE_INIT: self.settings.image_digest,
        }
        self._require_images(pod, everything, images)
        context = _field(spec, "securityContext")
        groups = _field(context, "supplementalGroups")
        if (
            not _host_namespaces_private(spec)
            or _field(spec, "shareProcessNamespace") is not False
            or _field(spec, "automountServiceAccountToken") is not False
            or not all(
                _is_worker_id(_field(context, name))
                for name in ("runAsUser", "runAsGroup", "fsGroup")
            )
            or _field(context, "runAsNonRoot") is not True
            or _field(context, "seccompProfile") != {"type": "RuntimeDefault"}
            or not (_none(groups) or groups == [_WORKER_ID])
            or not all(_confined(container) for container in everything)
        ):
            raise WorkerAttemptError("native worker confinement does not match admission")
        resources = _field(agent, "resources")
        limits = _field(resources, "limits")
        pod_resources = _field(spec, "resources")
        if (
            not all(_cpu_and_memory_only(_field(item, "resources")) for item in everything)
            or not (pod_resources is _ABSENT or _cpu_and_memory_only(pod_resources))
            or not _none(_field(spec, "resourceClaims"))
            or _observed_limits(limits) != _native_limits(self.settings.cpu, self.settings.memory)
            or _field(resources, "requests") != limits
        ):
            raise WorkerAttemptError("native worker resource limits do not match admission")
        self._inspect_volumes(spec, everything, sandbox_id)
        return sandbox

    def _require_images(
        self, pod: dict[str, Any], containers: tuple[dict[str, Any], ...], images: dict[str, str]
    ) -> None:
        """Require each container's requested and running image to be the pinned digest."""
        running = {
            item.get("name"): item.get("imageID")
            for group in ("containerStatuses", "initContainerStatuses")
            for item in _objects(_field(pod, "status"), group)
        }
        for container in containers:
            digest = images[container["name"]]
            if not _pinned(container.get("image"), digest) or not _pinned(
                running.get(container["name"]), digest
            ):
                raise WorkerAttemptError("native worker image does not match admission")

    def _inspect_volumes(
        self, spec: object, containers: tuple[dict[str, Any], ...], sandbox_id: str
    ) -> None:
        reported = _objects(spec, "volumes")
        volumes = {str(item.get("name")): item for item in reported}
        projections = {
            mount: {
                "name": mount,
                "persistentVolumeClaim": {"claimName": claim, "readOnly": True},
            }
            for mount, claim in self.names.items()
        }
        workspace = volumes.get("workspace", {})
        claim = _field(workspace, "persistentVolumeClaim")
        secret = _field(volumes.get(_RUNTIME_INIT), "secret", "secretName")
        if (
            len(volumes) != len(reported)
            or set(volumes) != {*projections, *_DRIVER_VOLUMES, "workspace"}
            # Any other volume type, such as a host path, is refused with its name.
            or any(set(volumes[name]) != {"name", kind} for name, kind in _DRIVER_VOLUMES.items())
            or set(workspace) != {"name", "persistentVolumeClaim"}
            or not isinstance(claim, dict)
            or set(claim) - {"claimName", "readOnly"}
            or claim.get("claimName") != "workspace-" + self.pod_name
            or not isinstance(secret, str)
            or not secret.startswith(f"os-sandbox-{sandbox_id}-")
        ):
            raise WorkerAttemptError("native worker has a volume that admission does not list")
        mounts = {
            container["name"]: _objects(container, "volumeMounts") for container in containers
        }
        if any(
            not isinstance(mount.get("mountPath"), str) or mount.get("name") not in volumes
            for observed_mounts in mounts.values()
            for mount in observed_mounts
        ):
            raise WorkerAttemptError("native worker projection mounts were not observed")
        # The bootstrap material is for the init container that installs the sandbox runtime.
        if any(
            mount.get("name") == _RUNTIME_INIT
            for name in (_AGENT, _WORKSPACE_INIT)
            for mount in mounts[name]
        ):
            raise WorkerAttemptError("native worker has a volume that admission does not list")
        for mount, volume in projections.items():
            target = PurePosixPath("/" + mount)
            matching = [
                observed
                for observed in mounts[_AGENT]
                if (path := PurePosixPath(observed["mountPath"])) == target
                or path.is_relative_to(target)
                or target.is_relative_to(path)
            ]
            if (
                volumes[mount] != volume
                or len(matching) != 1
                or matching[0].get("name") != mount
                or PurePosixPath(matching[0]["mountPath"]) != target
                or any(
                    observed.get("readOnly") is not True
                    for observed_mounts in mounts.values()
                    for observed in observed_mounts
                    if observed.get("name") == mount
                )
            ):
                raise WorkerAttemptError("native worker projection mount is not read-only")

    def _inspect_supervisor(
        self, pod: dict[str, Any], sandbox_id: str, sandbox: tuple[str, str]
    ) -> None:
        metadata, spec = _field(pod, "metadata"), _field(pod, "spec")
        labels = _field(metadata, "labels")
        expected = {_PAIR: sandbox_id, _ROLE: "supervisor", _SANDBOX: sandbox_id}
        if (
            not isinstance(labels, dict)
            or any(labels.get(name) != value for name, value in expected.items())
            or _sandbox_owner(metadata) != sandbox
        ):
            raise WorkerAttemptError("native supervisor pod does not belong to this sandbox")
        containers = _objects(spec, "containers")
        if (
            [item.get("name") for item in containers] != [_SUPERVISOR]
            or not _none(_field(spec, "initContainers"))
            or not _none(_field(spec, "ephemeralContainers"))
        ):
            raise WorkerAttemptError("native supervisor pod has unexpected containers")
        supervisor = containers[0]
        self._require_images(
            pod, (supervisor,), {_SUPERVISOR: self.settings.supervisor_image_digest}
        )
        pod_resources = _field(spec, "resources")
        if (
            not _host_namespaces_private(spec)
            or _field(spec, "automountServiceAccountToken") is not False
            or _field(spec, "securityContext", "seccompProfile") != {"type": "RuntimeDefault"}
            or not _confined(supervisor)
            or not _cpu_and_memory_only(_field(supervisor, "resources"))
            or not (pod_resources is _ABSENT or _cpu_and_memory_only(pod_resources))
            or not _none(_field(spec, "resourceClaims"))
            or not all(_supervisor_volume(volume) for volume in _objects(spec, "volumes"))
        ):
            raise WorkerAttemptError("native supervisor confinement does not match admission")

    def _inspect_policies(
        self, observed: dict[str, Any], sandbox_id: str, sandbox: tuple[str, str]
    ) -> None:
        control = observed["control_policy"]
        pair = {_PAIR: sandbox_id}
        if (
            _sandbox_owner(_field(control, "metadata")) != sandbox
            or _field(control, "spec")
            != {
                "egress": [
                    {
                        "ports": _CONTROL_PORT,
                        "to": [{"podSelector": {"matchLabels": {**pair, _ROLE: "supervisor"}}}],
                    }
                ],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": {**pair, _ROLE: "workload"}}}],
                        "ports": _CONTROL_PORT,
                    }
                ],
                "podSelector": {"matchLabels": pair},
                "policyTypes": ["Ingress", "Egress"],
            }
            or _field(observed["workload_policy"], "spec") != _FENCE_SPEC
        ):
            raise WorkerAttemptError("native worker network policies do not match admission")
