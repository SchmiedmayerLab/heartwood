# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Kubernetes staging keeps attempt ownership and preserves resources on uncertainty."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import pytest

from heartwood.core_adapter.worker_attempts import WorkerAttemptError, WorkerAttemptIntent
from heartwood.gateway import worker_kubernetes
from heartwood.gateway.worker_deployment import KubernetesWorkerDeployment
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.gateway.worker_kubernetes import KubernetesWorkerVolumes

_IMAGE = "sha256:" + "a" * 64
_NAMESPACE = "hw-s-synthetic"
_SANDBOX_ID = "0698f582-bdd8-4ddc-969c-96c0e749a85e"
_POLICY = "networkpolicy.networking.k8s.io"
_CLAIM = "persistentvolumeclaim"
_SANDBOX = "sandbox.agents.x-k8s.io"
_KINDS = {
    "pod": "Pod",
    _CLAIM: "PersistentVolumeClaim",
    _POLICY: "NetworkPolicy",
    _SANDBOX: "Sandbox",
}
_DENY_ALL = {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}


class _Cluster:
    """One namespace as `kubectl` reports and changes it, with every call recorded."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.copied: dict[str, dict[str, tuple[int, bytes | None]]] = {}
        # Members of each archive that standard input drops, as a stream cut at a boundary.
        self.dropped: dict[str, int] = {}
        self.extra: dict[str, dict[str, tuple[int, bytes | None]]] = {}
        self.links: dict[str, dict[str, str]] = {}
        self.reported_before = b""
        self.failing: str | None = None
        self.pod_phases = ["Pending", "Running"]
        self.terminating: dict[tuple[str, str], int] = {}
        self.uid = 0
        self.place(_POLICY, "default-deny", spec=_DENY_ALL)

    def place(
        self,
        resource: str,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        spec: dict[str, Any] | None = None,
        owners: list[str] | None = None,
    ) -> dict[str, Any]:
        self.uid += 1
        self.objects[resource, name] = {
            "apiVersion": "v1",
            "kind": _KINDS[resource],
            "metadata": {
                "name": name,
                "namespace": _NAMESPACE,
                "uid": f"uid-{self.uid}",
                "labels": dict(labels or {}),
                "ownerReferences": [{"kind": "Sandbox", "name": owner} for owner in owners or []],
            },
            "spec": spec or {},
            "status": {},
        }
        return self.objects[resource, name]

    def command(self, *arguments: str, stdin: bytes | None = None) -> bytes:
        self.calls.append(arguments)
        if self.failing is not None and self.failing in arguments:
            raise WorkerAttemptError("native Kubernetes operation requires reconciliation")
        verb = arguments[0]
        if verb == "get":
            return self._get(*arguments[1:])
        if verb == "create":
            assert arguments[1:] == ("--filename=-",)
            assert stdin is not None
            created = json.loads(stdin)
            resource = {kind: resource for resource, kind in _KINDS.items()}[created["kind"]]
            name = created["metadata"]["name"]
            assert created["metadata"]["namespace"] == _NAMESPACE
            if (resource, name) in self.objects:
                raise WorkerAttemptError("native Kubernetes operation requires reconciliation")
            self.uid += 1
            created["metadata"]["uid"] = f"uid-{self.uid}"
            created["status"] = {}
            self.objects[resource, name] = created
            return b""
        if verb == "exec" and "--stdin" not in arguments:
            return self._digest(*arguments[1:])
        if verb == "exec":
            assert stdin is not None
            pod = self.objects["pod", arguments[1]]
            assert arguments[2:8] == (
                "--container=stage",
                "--stdin",
                "--",
                "tar",
                "--extract",
                "--file=-",
            )
            assert pod["status"]["phase"] == "Running"
            mount = arguments[8].removeprefix("--directory=/")
            with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
                members = archive.getmembers()
                kept = members[: len(members) - self.dropped.get(mount, 0)]
                self.copied[mount] = {
                    member.name: (
                        member.mode,
                        None if member.isdir() else archive.extractfile(member).read(),  # type: ignore[union-attr]
                    )
                    for member in kept
                }
            return b""
        assert verb == "delete"
        resource, selector, field = arguments[1:4]
        assert arguments[4:] == ("--wait=false", "--ignore-not-found")
        required = dict(item.split("=") for item in selector.removeprefix("--selector=").split(","))
        name = field.removeprefix("--field-selector=metadata.name=")
        found = self.objects.get((resource, name))
        if (
            found is not None
            and required.items() <= found["metadata"]["labels"].items()
            and self.terminating.get((resource, name), 0) == 0
        ):
            del self.objects[resource, name]
        return b""

    def _digest(self, pod: str, *arguments: str) -> bytes:
        """Run the staging pod's digest script on the tree its copy created."""
        assert self.objects["pod", pod]["status"]["phase"] == "Running"
        container, separator, python, *flags, script, mount, lost_and_found = arguments
        assert (container, separator, python, flags) == (
            "--container=stage",
            "--",
            "/opt/heartwood/.venv/bin/python",
            ["-I", "-B", "-c"],
        )
        with TemporaryDirectory() as root:
            tree = Path(root)
            entries = {**self.copied[mount.removeprefix("/")], **self.extra.get(mount[1:], {})}
            for name, (mode, content) in sorted(entries.items()):
                path = tree / name
                if content is None:
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                path.chmod(mode)
            for name, target in self.links.get(mount[1:], {}).items():
                (tree / name).symlink_to(target)
            # The filesystem's own directory belongs to user 0; here, to the test's user.
            local = script.replace("owner = 0\n", f"owner = {os.getuid()}\n")
            assert local != script
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", local, root, lost_and_found],
                capture_output=True,
                check=False,
            )
        if result.returncode != 0:
            raise WorkerAttemptError("native Kubernetes operation requires reconciliation")
        return self.reported_before + result.stdout

    def _get(self, resource: str, *arguments: str) -> bytes:
        if arguments == ("--output=json",):
            items = [value for (kind, _), value in self.objects.items() if kind == resource]
            return json.dumps({"kind": "List", "items": items}).encode()
        name, *options = arguments
        found = self.objects.get((resource, name))
        if found is not None and self.terminating.get((resource, name), 0) > 0:
            self.terminating[resource, name] -= 1
            if self.terminating[resource, name] == 0:
                del self.objects[resource, name]
        if found is None:
            if options == ["--ignore-not-found", "--output=json"]:
                return b""
            raise WorkerAttemptError("native Kubernetes operation requires reconciliation")
        if (
            resource == "pod"
            and name.startswith("hw-staging-")
            and "--ignore-not-found" not in options
        ):
            if len(self.pod_phases) > 1:
                found["status"]["phase"] = self.pod_phases.pop(0)
            else:
                found["status"]["phase"] = self.pod_phases[0]
            found["status"]["containerStatuses"] = [{"name": "stage", "restartCount": 0}]
        return json.dumps(found).encode()

    def names(self, resource: str) -> list[str]:
        return sorted(name for kind, name in self.objects if kind == resource)

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]


def _settings(root: Path) -> KubernetesWorkerDeployment:
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
            "supervisor_image_digest": "sha256:" + "5" * 64,
            "sandbox_image_digest": "sha256:" + "6" * 64,
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
        }
    )


@pytest.fixture
def cluster() -> _Cluster:
    return _Cluster()


@pytest.fixture
def volumes(
    tmp_path: Path, cluster: _Cluster, monkeypatch: pytest.MonkeyPatch
) -> KubernetesWorkerVolumes:
    intent = WorkerAttemptIntent(
        project_id=uuid4(),
        session_id="synthetic",
        attempt_id=uuid4(),
        conversation_id=uuid4(),
        image_digest=_IMAGE,
        gateway_policy_sha256="b" * 64,
        sandbox_policy_sha256="c" * 64,
        input_manifest_sha256="d" * 64,
    )
    created = KubernetesWorkerVolumes(_settings(tmp_path), intent)
    monkeypatch.setattr(created, "command", cluster.command)
    monkeypatch.setattr(worker_kubernetes, "sleep", lambda _seconds: None)
    return created


@pytest.fixture
def sources(tmp_path: Path) -> dict[str, Path]:
    inputs, skills = tmp_path / "staged" / "inputs", tmp_path / "staged" / "skills"
    (inputs / "data").mkdir(parents=True, mode=0o700)
    (inputs / "data" / "table.csv").write_bytes(b"synthetic,1\n")
    (inputs / "data" / "table.csv").chmod(0o400)
    (skills / "synthetic").mkdir(parents=True, mode=0o700)
    (skills / "synthetic" / "SKILL.md").write_bytes(b"# Synthetic\n")
    (skills / "synthetic" / "run.sh").write_bytes(b"#!/bin/sh\n")
    (skills / "synthetic" / "run.sh").chmod(0o500)
    for directory in (inputs, skills, tmp_path / "staged"):
        directory.chmod(0o700)
    return {"inputs": inputs.resolve(), "skills": skills.resolve()}


def _worker(cluster: _Cluster, volumes: KubernetesWorkerVolumes) -> None:
    """Place the Sandbox resource and both pods as the driver creates them."""
    cluster.place(_SANDBOX, volumes.pod_name)
    cluster.place(
        "pod",
        volumes.pod_name,
        labels={**volumes.labels, "heartwood.role": "worker"},
        owners=[volumes.pod_name],
    )
    cluster.place(
        "pod",
        "os-supervisor-" + _SANDBOX_ID,
        labels={"openshell.ai/boundary-pair": _SANDBOX_ID},
        owners=[volumes.pod_name],
    )


def test_staging_copies_snapshots_into_labeled_claims_and_removes_its_pod(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    created: list[dict[str, Any]] = []
    command = cluster.command

    def record(*arguments: str, stdin: bytes | None = None) -> bytes:
        if arguments[0] == "create":
            assert stdin is not None
            created.append(json.loads(stdin))
        return command(*arguments, stdin=stdin)

    volumes.command = record  # type: ignore[method-assign]
    volumes.stage(sources)
    claims, pod = created[:2], created[2]
    assert [claim["metadata"]["name"] for claim in claims] == list(volumes.names.values())
    attempt = volumes.staging_name.removeprefix("hw-staging-")
    assert volumes.names == {
        mount: f"hw-attempt-{attempt}-{mount}" for mount in ("inputs", "skills")
    }
    for claim in claims:
        assert claim["metadata"]["labels"] == {
            **volumes.labels,
            "heartwood.role": "projection",
            "openshell.ai/sandbox-attachable": "true",
            "openshell.ai/sandbox-attachable-workspace": "hw",
        }
        assert claim["spec"] == {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "heartwood-attempt",
            "resources": {"requests": {"storage": "256Mi"}},
        }
    assert volumes.claims == {
        mount: cluster.objects[_CLAIM, name]["metadata"]["uid"]
        for mount, name in volumes.names.items()
    }
    assert pod["metadata"]["name"] == volumes.staging_name
    assert volumes.staging_name.startswith("hw-staging-")
    assert pod["metadata"]["labels"] == {**volumes.labels, "heartwood.role": "staging"}
    spec = pod["spec"]
    assert spec["serviceAccountName"] == "hw-staging"
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never"
    assert spec["terminationGracePeriodSeconds"] == 1
    assert spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10002,
        "runAsGroup": 10002,
        "fsGroup": 10002,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    (container,) = spec["containers"]
    assert container["image"] == "registry.test/heartwood/worker@" + _IMAGE
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert container["resources"]["limits"] == container["resources"]["requests"]
    # Only the attempt's claims are mounted: no host path, Secret or token volume.
    assert spec["volumes"] == [
        {"name": mount, "persistentVolumeClaim": {"claimName": name}}
        for mount, name in volumes.names.items()
    ]
    assert not {"hostNetwork", "hostPID", "hostIPC", "initContainers"} & set(spec)
    assert cluster.copied == {
        "inputs": {"data": (0o755, None), "data/table.csv": (0o644, b"synthetic,1\n")},
        "skills": {
            "synthetic": (0o755, None),
            "synthetic/SKILL.md": (0o644, b"# Synthetic\n"),
            # A file keeps whether it is executable.
            "synthetic/run.sh": (0o755, b"#!/bin/sh\n"),
        },
    }
    assert cluster.names("pod") == []
    assert cluster.names(_CLAIM) == sorted(volumes.names.values())
    with pytest.raises(WorkerAttemptError, match="cannot repeat"):
        volumes.stage(sources)


def test_driver_configuration_mounts_only_the_attempt_claims_read_only(
    volumes: KubernetesWorkerVolumes,
) -> None:
    assert volumes.driver_config() == {
        "volumes": [
            {"name": mount, "persistent_volume_claim": {"claim_name": name, "read_only": True}}
            for mount, name in volumes.names.items()
        ],
        "containers": {
            "agent": {
                "volume_mounts": [
                    {"name": mount, "mount_path": "/" + mount, "read_only": True}
                    for mount in ("inputs", "skills")
                ]
            }
        },
    }


@pytest.mark.parametrize("change", ["missing", "extra", "relative", "public", "link", "special"])
def test_snapshots_that_are_not_private_directories_of_files_reach_no_cluster_request(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], change: str
) -> None:
    if change == "missing":
        del sources["skills"]
    elif change == "extra":
        sources["data"] = sources["inputs"]
    elif change == "relative":
        sources["inputs"] = Path("staged/inputs")
    elif change == "public":
        sources["inputs"].chmod(0o755)
    elif change == "link":
        (sources["inputs"] / "alias").symlink_to("/etc/hosts")
    else:
        (sources["skills"] / "alias").symlink_to(sources["inputs"], target_is_directory=True)
    with pytest.raises(WorkerAttemptError, match="worker staging requires"):
        volumes.stage(sources)
    assert cluster.calls == []


@pytest.mark.parametrize(
    "policies",
    [
        [],
        [{"podSelector": {}, "policyTypes": ["Ingress"]}],
        [{"podSelector": {}, "policyTypes": ["Egress"]}],
        [
            {
                "podSelector": {"matchLabels": {"heartwood.role": "worker"}},
                "policyTypes": ["Ingress", "Egress"],
            }
        ],
        [_DENY_ALL, {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}],
        [
            _DENY_ALL,
            {
                "podSelector": {"matchLabels": {"heartwood.role": "staging"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {}}]}],
            },
        ],
        [
            _DENY_ALL,
            {
                "podSelector": {
                    "matchExpressions": [
                        {"key": "heartwood.role", "operator": "In", "values": ["staging"]}
                    ]
                },
                "policyTypes": ["Egress"],
                "egress": [{"ports": [{"port": 53}]}],
            },
        ],
        [
            _DENY_ALL,
            {
                "podSelector": {
                    "matchExpressions": [{"key": "openshell.ai/role", "operator": "DoesNotExist"}]
                },
                "policyTypes": ["Egress"],
                "egress": [{}],
            },
        ],
        [_DENY_ALL, {"policyTypes": ["Egress"], "egress": [{}]}],
        [
            _DENY_ALL,
            {
                "podSelector": {"matchExpressions": [{"key": "a", "operator": "Near"}]},
                "policyTypes": ["Egress"],
            },
        ],
        [_DENY_ALL, {"podSelector": {"matchLabels": ["heartwood.role"]}}],
        [_DENY_ALL, {"podSelector": {"matchExpressions": {"key": "a"}}}],
        [
            _DENY_ALL,
            {"podSelector": {"matchExpressions": [{"key": "a", "operator": "In", "values": "a"}]}},
        ],
        [_DENY_ALL, {"podSelector": {"matchExpressions": [{"operator": "Exists"}]}}],
        [_DENY_ALL, {"podSelector": {"labelSelector": {}}}],
        [{"podSelector": {}}],
        [{"podSelector": {}, "policyTypes": "Ingress,Egress"}],
    ],
)
def test_staging_requires_policies_that_deny_its_pod_all_traffic(
    volumes: KubernetesWorkerVolumes,
    cluster: _Cluster,
    sources: dict[str, Path],
    policies: list[dict[str, Any]],
) -> None:
    del cluster.objects[_POLICY, "default-deny"]
    for index, spec in enumerate(policies):
        cluster.place(_POLICY, f"policy-{index}", spec=spec)
    with pytest.raises(
        WorkerAttemptError, match=r"without network access|inspection was incomplete"
    ):
        volumes.stage(sources)
    assert set(cluster.verbs()) == {"get"}
    assert cluster.names("pod") == cluster.names(_CLAIM) == []


def test_policies_for_other_pods_do_not_stop_staging(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    for index, selector in enumerate(
        [
            {"matchLabels": {"openshell.ai/boundary-role": "supervisor"}},
            {
                "matchExpressions": [
                    {"key": "heartwood.role", "operator": "NotIn", "values": ["staging"]}
                ]
            },
            {"matchExpressions": [{"key": "app", "operator": "Exists"}]},
            {"matchExpressions": [{"key": "heartwood.role", "operator": "DoesNotExist"}]},
        ]
    ):
        cluster.place(
            _POLICY,
            f"other-{index}",
            spec={"podSelector": selector, "policyTypes": ["Egress"], "egress": [{}]},
        )
    volumes.stage(sources)
    assert cluster.names(_CLAIM) == sorted(volumes.names.values())


@pytest.mark.parametrize("existing", ["pod", "inputs", "skills"])
def test_staging_never_reuses_a_pod_or_claim_under_its_names(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], existing: str
) -> None:
    if existing == "pod":
        cluster.place("pod", volumes.staging_name, labels=volumes.labels)
    else:
        cluster.place(_CLAIM, volumes.names[existing], labels=volumes.labels)
    with pytest.raises(WorkerAttemptError, match="already exists"):
        volumes.stage(sources)
    assert set(cluster.verbs()) == {"get"}


@pytest.mark.parametrize("change", ["labels", "uid", "class", "dataSource", "dataSourceRef"])
def test_a_claim_the_cluster_reports_differently_stops_staging_before_the_pod(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], change: str
) -> None:
    command = cluster.command

    def altered(*arguments: str, stdin: bytes | None = None) -> bytes:
        output = command(*arguments, stdin=stdin)
        if arguments[:2] == ("get", _CLAIM) and "--ignore-not-found" not in arguments:
            claim = json.loads(output)
            if change == "labels":
                claim["metadata"]["labels"]["heartwood.attempt"] = str(uuid4())
            elif change == "uid":
                del claim["metadata"]["uid"]
            elif change == "class":
                claim["spec"]["storageClassName"] = "standard"
            else:
                claim["spec"][change] = {"kind": "PersistentVolumeClaim", "name": "project-data"}
            return json.dumps(claim).encode()
        return output

    volumes.command = altered  # type: ignore[method-assign]
    with pytest.raises(WorkerAttemptError, match=r"does not belong|does not match its request"):
        volumes.stage(sources)
    assert cluster.names("pod") == []
    assert "exec" not in cluster.verbs()


@pytest.mark.parametrize("phases", [["Failed"], ["Succeeded"], ["Unknown"], ["Pending"]])
def test_a_staging_pod_that_does_not_run_receives_no_copy(
    volumes: KubernetesWorkerVolumes,
    cluster: _Cluster,
    sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    phases: list[str],
) -> None:
    cluster.pod_phases = phases
    clock = iter(range(0, 100_000, 30))
    monkeypatch.setattr(worker_kubernetes, "monotonic", lambda: next(clock))
    with pytest.raises(WorkerAttemptError, match="did not complete"):
        volumes.stage(sources)
    assert "exec" not in cluster.verbs()
    # The uncertain resources stay for revocation, which removes them.
    assert cluster.names("pod") == [volumes.staging_name]
    volumes.remove_after_revocation()
    assert cluster.names("pod") == cluster.names(_CLAIM) == []


@pytest.mark.parametrize("failing", ["create", "exec", "delete"])
def test_a_failed_cluster_request_stops_staging_and_revocation_removes_what_exists(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], failing: str
) -> None:
    cluster.failing = failing
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        volumes.stage(sources)
    assert cluster.verbs().count(failing) == 1
    cluster.failing = None
    with pytest.raises(WorkerAttemptError, match="cannot repeat"):
        volumes.stage(sources)
    volumes.remove_after_revocation()
    assert cluster.names("pod") == cluster.names(_CLAIM) == []


def test_staging_compares_each_staged_tree_with_its_archive_in_the_pod(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    volumes.stage(sources)
    checks = [call for call in cluster.calls if call[0] == "exec" and "--stdin" not in call]
    assert [call[-2:] for call in checks] == [
        ("/inputs", "skip-lost-found"),
        ("/skills", "skip-lost-found"),
    ]
    # Each check follows the copy into the same claim.
    copies = [call for call in cluster.calls if call[0] == "exec"]
    assert [call[8] if "--stdin" in call else call[-2] for call in copies] == [
        "--directory=/inputs",
        "/inputs",
        "--directory=/skills",
        "/skills",
    ]


@pytest.mark.parametrize("change", ["truncated", "added", "mode"])
def test_a_staged_tree_that_differs_from_its_archive_stops_staging_before_the_worker(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], change: str
) -> None:
    if change == "truncated":
        # The stream ended at a member boundary, which tar accepts.
        cluster.dropped["inputs"] = 1
    elif change == "added":
        cluster.extra["inputs"] = {"planted.txt": (0o644, b"not staged\n")}
    else:
        cluster.extra["inputs"] = {"data/table.csv": (0o755, b"synthetic,1\n")}
    with pytest.raises(WorkerAttemptError, match="does not match the staged snapshot"):
        volumes.stage(sources)
    # Nothing follows the failed check: no further copy, and the pod stays for revocation.
    assert cluster.calls[-1][-2] == "/inputs"
    assert "skills" not in cluster.copied
    assert cluster.names("pod") == [volumes.staging_name]
    with pytest.raises(WorkerAttemptError, match="cannot repeat"):
        volumes.stage(sources)
    volumes.remove_after_revocation()
    assert cluster.names("pod") == cluster.names(_CLAIM) == []


def test_a_filesystem_recovery_directory_in_a_claim_is_not_staged_content(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    # Group permissions are what fsGroup adds to the filesystem's own directory.
    cluster.extra["inputs"] = {"lost+found": (0o770, None)}
    volumes.stage(sources)
    assert cluster.names("pod") == []


def test_a_recovery_directory_with_content_is_not_skipped(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    cluster.extra["inputs"] = {
        "lost+found": (0o770, None),
        "lost+found/recovered": (0o600, b"block\n"),
    }
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        volumes.stage(sources)
    assert "skills" not in cluster.copied


def test_a_recovery_directory_open_to_other_users_is_not_skipped(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    cluster.extra["inputs"] = {"lost+found": (0o755, None)}
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        volumes.stage(sources)
    assert "skills" not in cluster.copied


@pytest.mark.parametrize("change", ["link", "output"])
def test_a_claim_with_a_link_or_a_report_with_other_output_stops_staging(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], change: str
) -> None:
    if change == "link":
        cluster.links["inputs"] = {"data/link": "table.csv"}
        expected = "requires reconciliation"
    else:
        cluster.reported_before = b"warning: synthetic\n"
        expected = "does not match the staged snapshot"
    with pytest.raises(WorkerAttemptError, match=expected):
        volumes.stage(sources)
    assert "skills" not in cluster.copied
    assert cluster.names("pod") == [volumes.staging_name]


def test_a_snapshot_that_cannot_be_compared_stops_staging_before_any_request(
    volumes: KubernetesWorkerVolumes,
    cluster: _Cluster,
    sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unencodable(_source: Path) -> bytes:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo("bad\udcff")
            member.size = 0
            archive.addfile(member, io.BytesIO(b""))
        return stream.getvalue()

    monkeypatch.setattr(worker_kubernetes, "_archive", unencodable)
    with pytest.raises(WorkerAttemptError, match="canonical private snapshots"):
        volumes.stage(sources)
    assert cluster.calls == []


def test_a_snapshot_entry_named_like_the_recovery_directory_is_compared(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    (sources["inputs"] / "lost+found").mkdir(mode=0o700)
    cluster.extra["inputs"] = {"lost+found/planted.txt": (0o644, b"not staged\n")}
    with pytest.raises(WorkerAttemptError, match="does not match the staged snapshot"):
        volumes.stage(sources)


def test_the_pod_digest_script_reports_the_digest_of_an_extracted_archive(
    tmp_path: Path, sources: dict[str, Path]
) -> None:
    archive = worker_kubernetes._archive(sources["skills"])
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as members:
        members.extractall(extracted, filter="fully_trusted")
    reported = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            worker_kubernetes._TREE_DIGEST_SCRIPT,
            str(extracted),
            "skip-lost-found",
        ],
        capture_output=True,
        check=True,
    )
    assert reported.stdout.decode().strip() == worker_kubernetes._tree_digest(archive)
    (extracted / "synthetic" / "link").symlink_to("run.sh")
    refused = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            worker_kubernetes._TREE_DIGEST_SCRIPT,
            str(extracted),
            "keep-lost-found",
        ],
        capture_output=True,
        check=False,
    )
    assert refused.returncode != 0
    assert refused.stdout == b""


@pytest.mark.parametrize("change", ["replaced", "restarted", "ended", "relabeled"])
def test_a_staging_pod_that_changed_during_the_copy_is_not_accepted(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], change: str
) -> None:
    command = cluster.command

    def altered(*arguments: str, stdin: bytes | None = None) -> bytes:
        output = command(*arguments, stdin=stdin)
        if arguments[:2] == ("get", "pod") and "exec" in cluster.verbs() and output:
            pod = json.loads(output)
            if change == "replaced":
                pod["metadata"]["uid"] = "another"
            elif change == "restarted":
                pod["status"]["containerStatuses"][0]["restartCount"] = 1
            elif change == "ended":
                pod["status"]["phase"] = "Succeeded"
            else:
                pod["metadata"]["labels"]["heartwood.role"] = "worker"
            return json.dumps(pod).encode()
        return output

    volumes.command = altered  # type: ignore[method-assign]
    with pytest.raises(WorkerAttemptError, match=r"does not belong|did not complete"):
        volumes.stage(sources)
    assert "delete" not in cluster.verbs()


def test_revocation_removes_claims_only_after_the_worker_is_absent(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    volumes.stage(sources)
    cluster.calls.clear()
    volumes.remove_after_revocation(_SANDBOX_ID)
    assert cluster.names(_CLAIM) == []
    reads = [call for call in cluster.calls if call[0] == "get"]
    assert reads[0][1:3] == (_SANDBOX, volumes.pod_name)
    assert reads[1] == ("get", "pod", "--output=json")
    first_delete = cluster.verbs().index("delete")
    assert first_delete > 1
    volumes.remove_after_revocation(_SANDBOX_ID)


@pytest.mark.parametrize(
    "remaining",
    [
        "sandbox",
        "workload-name",
        "supervisor-name",
        "workload-labels",
        "owned",
        "pair",
        "sandbox-label",
    ],
)
def test_claims_stay_while_any_object_of_the_worker_remains(
    volumes: KubernetesWorkerVolumes,
    cluster: _Cluster,
    sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    remaining: str,
) -> None:
    volumes.stage(sources)
    if remaining == "sandbox":
        cluster.place(_SANDBOX, volumes.pod_name)
    elif remaining == "workload-name":
        cluster.place("pod", volumes.pod_name)
    elif remaining == "supervisor-name":
        cluster.place("pod", "os-supervisor-" + _SANDBOX_ID)
    elif remaining == "workload-labels":
        cluster.place("pod", "renamed", labels={**volumes.labels, "heartwood.role": "worker"})
    elif remaining == "owned":
        cluster.place("pod", "os-supervisor-unknown", owners=[volumes.pod_name])
    elif remaining == "pair":
        cluster.place("pod", "unowned", labels={"openshell.ai/boundary-pair": _SANDBOX_ID})
    else:
        cluster.place("pod", "unowned", labels={"openshell.ai/sandbox-id": _SANDBOX_ID})
    clock = iter(range(0, 100_000, 30))
    monkeypatch.setattr(worker_kubernetes, "monotonic", lambda: next(clock))
    with pytest.raises(WorkerAttemptError, match="remains after control-plane revocation"):
        volumes.remove_after_revocation(_SANDBOX_ID)
    assert cluster.verbs().count("delete") == 1
    assert cluster.names(_CLAIM) == sorted(volumes.names.values())


def test_pods_of_other_sandboxes_and_attempts_do_not_hold_the_claims(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path]
) -> None:
    volumes.stage(sources)
    cluster.place("pod", "openshell-0", labels={"app.kubernetes.io/name": "openshell"})
    cluster.place(
        "pod",
        "hw--hw-another",
        labels={**volumes.labels, "heartwood.attempt": str(uuid4()), "heartwood.role": "worker"},
        owners=["hw--hw-another"],
    )
    cluster.place("pod", "os-supervisor-other", labels={"openshell.ai/boundary-pair": "other"})
    volumes.remove_after_revocation(_SANDBOX_ID)
    assert cluster.names(_CLAIM) == []
    assert len(cluster.names("pod")) == 3


def test_revocation_waits_for_a_supervisor_pod_that_is_still_terminating(
    volumes: KubernetesWorkerVolumes,
    cluster: _Cluster,
    sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volumes.stage(sources)
    _worker(cluster, volumes)
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 1:
            del cluster.objects[_SANDBOX, volumes.pod_name]
            del cluster.objects["pod", volumes.pod_name]
        if len(waits) == 3:
            del cluster.objects["pod", "os-supervisor-" + _SANDBOX_ID]

    monkeypatch.setattr(worker_kubernetes, "sleep", wait)
    volumes.remove_after_revocation(_SANDBOX_ID)
    assert waits == [1, 1, 1]
    assert cluster.names(_CLAIM) == cluster.names("pod") == []


@pytest.mark.parametrize("resource", ["pod", _CLAIM])
def test_a_foreign_object_under_an_attempt_name_is_never_deleted(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, resource: str
) -> None:
    name = volumes.staging_name if resource == "pod" else volumes.names["inputs"]
    cluster.place(resource, name, labels={"heartwood.role": "staging"})
    with pytest.raises(WorkerAttemptError, match="does not belong"):
        volumes.remove_after_revocation()
    assert "delete" not in cluster.verbs()
    # The deletion request itself names the complete identity, so a replaced object stays.
    volumes._delete(resource, name, role="projection")
    assert cluster.names(resource) == [name]


def test_an_object_that_stays_after_its_deletion_is_not_reported_as_removed(
    volumes: KubernetesWorkerVolumes,
    cluster: _Cluster,
    sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volumes.stage(sources)
    cluster.terminating[_CLAIM, volumes.names["skills"]] = 3
    volumes.remove_after_revocation()
    assert cluster.names(_CLAIM) == []
    cluster.place(
        _CLAIM, volumes.names["inputs"], labels={**volumes.labels, "heartwood.role": "projection"}
    )
    cluster.terminating[_CLAIM, volumes.names["inputs"]] = 1_000_000
    clock = iter(range(0, 100_000, 30))
    monkeypatch.setattr(worker_kubernetes, "monotonic", lambda: next(clock))
    with pytest.raises(WorkerAttemptError, match="remains after its deletion"):
        volumes.remove_after_revocation()


@pytest.mark.parametrize(
    "response",
    [
        b"invalid",
        b"[]",
        b"{}",
        b'{"kind":"Status","status":"Failure"}',
        b'{"kind":"List"}',
        b'{"items":[]}',
        b'{"kind":"List","items":{}}',
        b'{"kind":"List","items":[1]}',
        "namespace",
    ],
)
def test_an_unreadable_pod_listing_is_never_read_as_absence(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, response: str | bytes
) -> None:
    command = cluster.command

    def altered(*arguments: str, stdin: bytes | None = None) -> bytes:
        if arguments == ("get", "pod", "--output=json"):
            if isinstance(response, bytes):
                return response
            pod = cluster.place("pod", "elsewhere")
            pod["metadata"]["namespace"] = "another"
        return command(*arguments, stdin=stdin)

    volumes.command = altered  # type: ignore[method-assign]
    with pytest.raises(WorkerAttemptError, match="inspection was incomplete"):
        volumes.remove_after_revocation()


@pytest.mark.parametrize("failing", [2, 3])
def test_a_later_creation_that_fails_leaves_the_earlier_objects_for_revocation(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], failing: int
) -> None:
    command, creations = cluster.command, [0]

    def limited(*arguments: str, stdin: bytes | None = None) -> bytes:
        if arguments[0] == "create":
            creations[0] += 1
            if creations[0] == failing:
                raise WorkerAttemptError("native Kubernetes operation requires reconciliation")
        return command(*arguments, stdin=stdin)

    volumes.command = limited  # type: ignore[method-assign]
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        volumes.stage(sources)
    # The second claim, or the pod after both claims, was refused.
    assert len(cluster.names(_CLAIM)) == failing - 1
    assert cluster.names("pod") == []
    assert "exec" not in cluster.verbs()
    volumes.command = command  # type: ignore[method-assign]
    volumes.remove_after_revocation()
    assert cluster.names(_CLAIM) == []


@pytest.mark.parametrize("unreadable", [_SANDBOX, "pod"])
def test_absence_that_cannot_be_read_removes_nothing(
    volumes: KubernetesWorkerVolumes, cluster: _Cluster, sources: dict[str, Path], unreadable: str
) -> None:
    volumes.stage(sources)
    cluster.calls.clear()
    cluster.failing = unreadable
    with pytest.raises(WorkerAttemptError, match="requires reconciliation"):
        volumes.remove_after_revocation(_SANDBOX_ID)
    assert "delete" not in cluster.verbs()
    assert cluster.names(_CLAIM) == sorted(volumes.names.values())


_FAKE_KUBECTL = """#!{python} -I
import json, sys
from pathlib import Path
root = Path(__file__).parent
received = sys.stdin.buffer.read()
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": received.decode("latin-1")}}) + "\\n")
"""


def test_requests_reach_the_explicit_tool_with_their_input_and_nothing_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.kubectl_cli.write_text(_FAKE_KUBECTL.format(python=sys.executable))
    settings.kubectl_cli.chmod(0o700)
    monkeypatch.setattr("sys.stdin", io.StringIO("ambient input"))
    assert (
        worker_kubernetes._kubectl(settings, "create", "--filename=-", stdin=b"\x00object") == b""
    )
    assert worker_kubernetes._kubectl(settings, "get", "pod") == b""
    first, second = (
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    )
    assert first["argv"][-2:] == ["create", "--filename=-"]
    assert first["stdin"] == "\x00object"
    assert second["stdin"] == ""
