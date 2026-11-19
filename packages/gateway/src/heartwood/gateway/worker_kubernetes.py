# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Staging, admission and evidence of absence of a Kubernetes worker through `kubectl`."""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Any

from heartwood.core_adapter.worker_attempts import WorkerAttemptError, WorkerAttemptIntent
from heartwood.gateway.worker_deployment import KubernetesWorkerDeployment
from heartwood.gateway.worker_volumes import _native_limits, require_private_snapshots

_WORKER_ID = 10001
_PAIR = "openshell.ai/boundary-pair"
_ROLE = "openshell.ai/boundary-role"
_SANDBOX = "openshell.ai/sandbox-id"
_POD_SECURITY = "pod-security.kubernetes.io/enforce"
_APPARMOR_ANNOTATION = "container.apparmor.security.beta.kubernetes.io/"
# Every security context field the driver sets. Any other field is refused, so a profile,
# label or option that could replace a default, such as an unconfined AppArmor profile,
# SELinux options or an unmasked `/proc`, never passes unread.
_POD_CONTEXT_FIELDS = frozenset(
    {
        "fsGroup",
        "fsGroupChangePolicy",
        "runAsGroup",
        "runAsNonRoot",
        "runAsUser",
        "seccompProfile",
        "supplementalGroups",
        "supplementalGroupsPolicy",
        "sysctls",
    }
)
_CONTAINER_CONTEXT_FIELDS = frozenset(
    {
        "allowPrivilegeEscalation",
        "capabilities",
        "privileged",
        "readOnlyRootFilesystem",
        "runAsGroup",
        "runAsNonRoot",
        "runAsUser",
        "seccompProfile",
    }
)
# The one kernel setting the driver's workload pod carries.
_SYSCTLS = [{"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"}]
_MOUNT_FIELDS = frozenset({"name", "mountPath", "readOnly", "recursiveReadOnly"})
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
# The gateway's certificate authority, which the supervisor verifies the gateway with.
_GATEWAY_TLS_SECRET = "openshell-client-tls"
_SANDBOX_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
_CPU = re.compile(r"([0-9]+)(m?)")
_MEMORY = re.compile(r"([0-9]+)(Ki|Mi|Gi|Ti)?")
_MEMORY_UNITS = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}

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
    "runtimeClassName",
    "volumes",
)
_RECORDED_CONTAINER_FIELDS = (
    "name",
    "image",
    "securityContext",
    "resources",
    "volumeMounts",
    "volumeDevices",
    "ports",
    "restartPolicy",
)
_ABSENT_RECORD = "<absent>"

# What the platform provides in the namespace for staging: a service account that grants
# nothing, and the storage class of the attempt's claims.
_STAGING_SERVICE_ACCOUNT = "hw-staging"
_STAGING_CONTAINER = "stage"
_STORAGE_CLASS = "heartwood-attempt"
# Inputs and Skills are each limited to 64 MiB before staging.
_CLAIM_STORAGE = "256Mi"
_STAGING_LIMITS = {"cpu": "1", "memory": "256Mi"}
# The staging pod ends itself after this time if the gateway process ends first.
_STAGING_SECONDS = 600
_STAGING_READY_SECONDS = 120
_REMOVAL_SECONDS = 60
# The supervisor pod can stay for its termination grace period after the sandbox gateway
# reports the deletion.
_ABSENCE_SECONDS = 90
_POLICY = "networkpolicy.networking.k8s.io"
_CLAIM = "persistentvolumeclaim"
_SANDBOX_RESOURCE = "sandbox.agents.x-k8s.io"


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


def _pinned(reference: object, digest: str) -> bool:
    """Accept only an image reference by digest, never a tag that could move."""
    return (
        isinstance(reference, str)
        and reference.endswith("@" + digest)
        and len(reference) > len(digest) + 1
    )


def _confined(container: dict[str, Any]) -> bool:
    context = _field(container, "securityContext")
    capabilities = _field(context, "capabilities")
    drop = _field(capabilities, "drop")
    # A container profile replaces the pod's, so only the same profile is accepted.
    profile = _field(context, "seccompProfile")
    return (
        isinstance(context, dict)
        and set(context) <= _CONTAINER_CONTEXT_FIELDS
        and isinstance(capabilities, dict)
        and set(capabilities) <= {"add", "drop"}
        and _field(context, "allowPrivilegeEscalation") is False
        and _default(_field(context, "privileged"), False)
        and isinstance(drop, list)
        and "ALL" in drop
        and _none(_field(context, "capabilities", "add"))
        and _is_worker_id(_field(context, "runAsUser"))
        and _is_worker_id(_field(context, "runAsGroup"))
        and _field(context, "runAsNonRoot") is True
        and (profile is _ABSENT or profile == {"type": "RuntimeDefault"})
        and _none(_field(container, "volumeDevices"))
        # A host port would expose the container on the node's own address.
        and not any({"hostPort", "hostIP"} & set(port) for port in _objects(container, "ports"))
        # A reference would deliver another Secret or ConfigMap of the namespace.
        and _none(_field(container, "envFrom"))
        and not any("valueFrom" in variable for variable in _objects(container, "env"))
        # An init container that restarts keeps running next to the worker.
        and _field(container, "restartPolicy") is _ABSENT
    )


def _pod_confined(pod: object) -> bool:
    """Accept only the driver's pod security fields, the default profile and its runtime."""
    spec = _field(pod, "spec")
    context = _field(spec, "securityContext")
    annotations = _field(pod, "metadata", "annotations")
    sysctls = _field(context, "sysctls")
    groups = _field(context, "supplementalGroups")
    return (
        _host_namespaces_private(spec)
        and _field(spec, "automountServiceAccountToken") is False
        and isinstance(context, dict)
        and set(context) <= _POD_CONTEXT_FIELDS
        and context.get("seccompProfile") == {"type": "RuntimeDefault"}
        # The file system group joins every container's groups.
        and _is_worker_id(context.get("fsGroup"))
        and (_none(groups) or groups == [_WORKER_ID])
        and all(
            name not in context or _is_worker_id(context[name])
            for name in ("runAsUser", "runAsGroup")
        )
        and (sysctls is _ABSENT or sysctls == _SYSCTLS)
        and _field(spec, "runtimeClassName") is _ABSENT
        and (annotations is _ABSENT or isinstance(annotations, dict))
        and not any(
            str(name).startswith(_APPARMOR_ANNOTATION)
            for name in (annotations if isinstance(annotations, dict) else ())
        )
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


def _supervisor_volume(volume: dict[str, Any], sandbox_id: str) -> bool:
    """Accept the driver's volume types, this sandbox's Secrets and a gateway token only."""
    kinds = set(volume) - {"name"}
    if len(kinds) != 1 or not kinds <= _SUPERVISOR_VOLUME_TYPES:
        return False
    if "secret" in volume:
        secret = _field(volume, "secret", "secretName")
        return isinstance(secret, str) and (
            secret == _GATEWAY_TLS_SECRET or secret.startswith(f"os-supervisor-{sandbox_id}-")
        )
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


def _kubectl(settings: KubernetesWorkerDeployment, *args: str, stdin: bytes | None = None) -> bytes:
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
                # Without input the tool reads nothing from the gateway's own standard input.
                input=b"" if stdin is None else stdin,
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


def _parsed(output: bytes) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerAttemptError("native resource inspection was incomplete") from None
    if not isinstance(value, dict):
        raise WorkerAttemptError("native resource inspection was incomplete")
    return value


def _selects(selector: object, labels: dict[str, str]) -> bool:
    """Decide whether a label selector selects a pod with these labels, as the API does."""
    incomplete = WorkerAttemptError("native resource inspection was incomplete")
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        raise incomplete
    required = selector.get("matchLabels", {})
    if not isinstance(required, dict):
        raise incomplete
    if any(labels.get(name) != value for name, value in required.items()):
        return False
    for expression in _objects(selector, "matchExpressions"):
        name, operator = expression.get("key"), expression.get("operator")
        values = expression.get("values", [])
        if not isinstance(name, str) or not isinstance(values, list):
            raise incomplete
        if operator in ("In", "NotIn"):
            selected = (labels.get(name) in values) == (operator == "In")
        elif operator in ("Exists", "DoesNotExist"):
            selected = (name in labels) == (operator == "Exists")
        else:
            raise incomplete
        if not selected:
            return False
    return True


def _allows_traffic(policy: dict[str, Any]) -> bool:
    """Report a policy with any ingress or egress rule; a rule can only allow traffic."""
    spec = _field(policy, "spec")
    return bool(_objects(spec, "ingress")) or bool(_objects(spec, "egress"))


def _archive(source: Path) -> bytes:
    """Pack a staged snapshot's directories and regular files, readable by the worker."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(source.rglob("*")):
            directory = path.is_dir() and not path.is_symlink()
            if not directory and not (path.is_file() and not path.is_symlink()):
                raise WorkerAttemptError("worker staging requires canonical private snapshots")
            member = tarfile.TarInfo(path.relative_to(source).as_posix())
            member.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
            member.mode = 0o755 if directory else 0o644
            if directory:
                archive.addfile(member)
                continue
            content = path.read_bytes()
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


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

    def command(self, *args: str, stdin: bytes | None = None) -> bytes:
        """Use the explicit cluster configuration without inherited Kubernetes settings."""
        return _kubectl(self.settings, *args, stdin=stdin)

    def _get(self, resource: str, kind: str, name: str) -> dict[str, Any]:
        """Read one object; a missing, unreadable or differently named object is refused."""
        return self._object(self.command("get", resource, name, "--output=json"), kind, name)

    def _object(self, output: bytes, kind: str, name: str) -> dict[str, Any]:
        value = _parsed(output)
        if (
            value.get("kind") != kind
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
        version = _field(namespace, "metadata", "labels", _POD_SECURITY + "-version")
        if (
            _field(namespace, "metadata", "labels", _POD_SECURITY) != "restricted"
            # Without the label the cluster applies the latest version of the level.
            or not (version is _ABSENT or version == "latest")
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
        if (
            not _pod_confined(pod)
            or _field(spec, "shareProcessNamespace") is not False
            or not all(_is_worker_id(_field(context, name)) for name in ("runAsUser", "runAsGroup"))
            or _field(context, "runAsNonRoot") is not True
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
            name: item.get("imageID")
            for group in ("containerStatuses", "initContainerStatuses")
            for item in _objects(_field(pod, "status"), group)
            if isinstance(name := item.get("name"), str)
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
        # A path that is relative or not normalized could reach a projection unnoticed.
        if any(
            not isinstance(path := mount.get("mountPath"), str)
            or not path.startswith("/")
            or path.startswith("//")
            or posixpath.normpath(path) != path
            or not isinstance(name := mount.get("name"), str)
            or name not in volumes
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
                # A sub-path or propagation setting would change what the mount shows.
                or set(matching[0]) - _MOUNT_FIELDS
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
            not _pod_confined(pod)
            or not _confined(supervisor)
            or not _cpu_and_memory_only(_field(supervisor, "resources"))
            or not (pod_resources is _ABSENT or _cpu_and_memory_only(pod_resources))
            or not _none(_field(spec, "resourceClaims"))
            or not all(
                _supervisor_volume(volume, sandbox_id) for volume in _objects(spec, "volumes")
            )
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


class KubernetesWorkerVolumes(KubernetesWorkerAdmission):
    """Own the attempt's claims and staging pod, and observe its worker's absence."""

    def __init__(self, settings: KubernetesWorkerDeployment, intent: WorkerAttemptIntent) -> None:
        """Retain identities before provisioning any Kubernetes resource."""
        super().__init__(settings, intent)
        self._staging_attempted = False
        self.staging_name = f"hw-staging-{intent.attempt_id.hex}"
        # The identity the cluster gave each claim of this attempt, by mount.
        self.claims: dict[str, str] = {}

    def _find(self, resource: str, kind: str, name: str) -> dict[str, Any] | None:
        """Read one object, or nothing when the cluster answers that it does not exist."""
        output = self.command("get", resource, name, "--ignore-not-found", "--output=json")
        return self._object(output, kind, name) if output.strip() else None

    def _list(self, resource: str) -> list[dict[str, Any]]:
        """Read every object of one kind in the namespace."""
        items = _objects(_parsed(self.command("get", resource, "--output=json")), "items")
        if any(_field(item, "metadata", "namespace") != self.settings.namespace for item in items):
            raise WorkerAttemptError("native resource inspection was incomplete")
        return items

    def _owned(self, observed: dict[str, Any], *, role: str) -> str:
        """Require the attempt's complete identity and return the object's own identity."""
        labels, uid = _field(observed, "metadata", "labels"), _field(observed, "metadata", "uid")
        expected = {**self.labels, "heartwood.role": role}
        if (
            not isinstance(labels, dict)
            or any(labels.get(name) != value for name, value in expected.items())
            or not isinstance(uid, str)
            or not uid
        ):
            raise WorkerAttemptError("native resource does not belong to this worker attempt")
        return uid

    def _delete(self, resource: str, name: str, *, role: str) -> None:
        """Request deletion of one object only while it carries the attempt's identity."""
        selector = ",".join(
            f"{label}={value}" for label, value in {**self.labels, "heartwood.role": role}.items()
        )
        self.command(
            "delete",
            resource,
            "--selector=" + selector,
            "--field-selector=metadata.name=" + name,
            "--wait=false",
            "--ignore-not-found",
        )

    def _await_removal(self, resource: str, kind: str, names: tuple[str, ...]) -> None:
        deadline = monotonic() + _REMOVAL_SECONDS
        while any(self._find(resource, kind, name) is not None for name in names):
            if monotonic() >= deadline:
                raise WorkerAttemptError("native resource remains after its deletion")
            sleep(0.5)

    def _require_isolation(self, labels: dict[str, str]) -> None:
        """Require the namespace's policies to deny a pod with these labels all traffic."""
        selecting = [
            policy
            for policy in self._list(_POLICY)
            if _selects(_field(policy, "spec", "podSelector"), labels)
        ]
        denied = {
            kind
            for policy in selecting
            for kind in ("Ingress", "Egress")
            if isinstance(types := _field(policy, "spec", "policyTypes"), list) and kind in types
        }
        if denied != {"Ingress", "Egress"} or any(_allows_traffic(policy) for policy in selecting):
            raise WorkerAttemptError("worker staging requires a pod without network access")

    def _claim(self, name: str) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": name,
                "namespace": self.settings.namespace,
                "labels": {
                    **self.labels,
                    "heartwood.role": "projection",
                    # The sandbox gateway attaches only claims approved for its workspace.
                    "openshell.ai/sandbox-attachable": "true",
                    "openshell.ai/sandbox-attachable-workspace": self.settings.workspace,
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": _STORAGE_CLASS,
                "resources": {"requests": {"storage": _CLAIM_STORAGE}},
            },
        }

    def _staging_pod(self) -> dict[str, Any]:
        """A pod that only holds the attempt's claims while the gateway copies into them."""
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": self.staging_name,
                "namespace": self.settings.namespace,
                "labels": {**self.labels, "heartwood.role": "staging"},
            },
            "spec": {
                "restartPolicy": "Never",
                "activeDeadlineSeconds": _STAGING_SECONDS,
                "terminationGracePeriodSeconds": 0,
                "serviceAccountName": _STAGING_SERVICE_ACCOUNT,
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                # The worker's own identity, so the staged files need no change of owner.
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": _WORKER_ID,
                    "runAsGroup": _WORKER_ID,
                    "fsGroup": _WORKER_ID,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": _STAGING_CONTAINER,
                        "image": self.settings.image,
                        "command": ["/bin/sleep", str(_STAGING_SECONDS)],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "readOnlyRootFilesystem": True,
                        },
                        "resources": {"limits": _STAGING_LIMITS, "requests": _STAGING_LIMITS},
                        "volumeMounts": [
                            {"name": mount, "mountPath": f"/{mount}"} for mount in self.names
                        ],
                    }
                ],
                "volumes": [
                    {"name": mount, "persistentVolumeClaim": {"claimName": claim}}
                    for mount, claim in self.names.items()
                ],
            },
        }

    def _running_staging_pod(self) -> str:
        """Wait until the staging pod runs and return its identity."""
        deadline = monotonic() + _STAGING_READY_SECONDS
        while True:
            pod = self._get("pod", "Pod", self.staging_name)
            uid = self._owned(pod, role="staging")
            phase = _field(pod, "status", "phase")
            if phase == "Running":
                return uid
            if phase != "Pending" or monotonic() >= deadline:
                raise WorkerAttemptError("worker projection preparation did not complete")
            sleep(0.5)

    def stage(self, sources: dict[str, Path]) -> None:
        """Copy verified private snapshots before the untrusted worker is created."""
        if self._staging_attempted:
            raise WorkerAttemptError("worker staging cannot repeat an uncertain transfer")
        self._staging_attempted = True
        require_private_snapshots(sources, self.names)
        archives = {mount: _archive(source) for mount, source in sources.items()}
        self._require_isolation({**self.labels, "heartwood.role": "staging"})
        if self._find("pod", "Pod", self.staging_name) is not None:
            raise WorkerAttemptError("worker staging pod already exists")
        for name in self.names.values():
            if self._find(_CLAIM, "PersistentVolumeClaim", name) is not None:
                raise WorkerAttemptError("worker projection volume already exists")
        for mount, name in self.names.items():
            self.command("create", "--filename=-", stdin=json.dumps(self._claim(name)).encode())
            claim = self._get(_CLAIM, "PersistentVolumeClaim", name)
            self.claims[mount] = self._owned(claim, role="projection")
            # A claim copied from another claim or snapshot would carry its content.
            if _field(claim, "spec", "storageClassName") != _STORAGE_CLASS or any(
                _field(claim, "spec", source) is not _ABSENT
                for source in ("dataSource", "dataSourceRef")
            ):
                raise WorkerAttemptError("worker projection volume does not match its request")
        self.command("create", "--filename=-", stdin=json.dumps(self._staging_pod()).encode())
        staging = self._running_staging_pod()
        for mount, archive in archives.items():
            self.command(
                "exec",
                self.staging_name,
                "--container=" + _STAGING_CONTAINER,
                "--stdin",
                "--",
                "tar",
                "--extract",
                "--file=-",
                "--directory=/" + mount,
                stdin=archive,
            )
        finished = self._get("pod", "Pod", self.staging_name)
        statuses = _objects(_field(finished, "status"), "containerStatuses")
        if (
            self._owned(finished, role="staging") != staging
            or _field(finished, "status", "phase") != "Running"
            or [status.get("restartCount") for status in statuses] != [0]
        ):
            raise WorkerAttemptError("worker projection preparation did not complete")
        self._remove_staging()

    def driver_config(self) -> dict[str, Any]:
        """Supply only the two attempt-owned claims, read-only on the volume and the mount."""
        return {
            "volumes": [
                {"name": mount, "persistent_volume_claim": {"claim_name": claim, "read_only": True}}
                for mount, claim in self.names.items()
            ],
            "containers": {
                _AGENT: {
                    "volume_mounts": [
                        {"name": mount, "mount_path": f"/{mount}", "read_only": True}
                        for mount in self.names
                    ]
                }
            },
        }

    def _remove_staging(self) -> None:
        pod = self._find("pod", "Pod", self.staging_name)
        if pod is None:
            return
        self._owned(pod, role="staging")
        self._delete("pod", self.staging_name, role="staging")
        self._await_removal("pod", "Pod", (self.staging_name,))

    def _worker_remains(self, sandbox_id: str | None) -> bool:
        """Report the Sandbox resource or any pod of the attempt, by every identity they carry."""
        if self._find(_SANDBOX_RESOURCE, "Sandbox", self.pod_name) is not None:
            return True
        for pod in self._list("pod"):
            metadata = _field(pod, "metadata")
            labels = _field(metadata, "labels")
            labels = labels if isinstance(labels, dict) else {}
            owners = [owner.get("name") for owner in _objects(metadata, "ownerReferences")]
            if (
                _field(metadata, "name") == self.pod_name
                or self.pod_name in owners
                or (
                    sandbox_id is not None
                    and sandbox_id in (labels.get(_PAIR), labels.get(_SANDBOX))
                )
                or (
                    all(labels.get(name) == value for name, value in self.labels.items())
                    and labels.get("heartwood.role") != "staging"
                )
            ):
                return True
        return False

    def remove_after_revocation(self, sandbox_id: str | None = None) -> None:
        """Require independently observed worker absence before removing owned projections."""
        deadline = monotonic() + _ABSENCE_SECONDS
        while self._worker_remains(sandbox_id):
            if monotonic() >= deadline:
                raise WorkerAttemptError("native worker remains after control-plane revocation")
            sleep(1)
        self._remove_staging()
        for name in self.names.values():
            claim = self._find(_CLAIM, "PersistentVolumeClaim", name)
            if claim is None:
                continue
            self._owned(claim, role="projection")
            self._delete(_CLAIM, name, role="projection")
        self._await_removal(_CLAIM, "PersistentVolumeClaim", tuple(self.names.values()))
