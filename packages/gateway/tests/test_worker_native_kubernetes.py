# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Launch and revocation through the Kubernetes driver fail closed on every observation."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptLease,
    WorkerControlPlaneError,
)
from heartwood.gateway import ProjectContext, ProjectStateError, worker_native
from heartwood.gateway.worker_control import (
    ABSENCE_REJECTS_CREATION,
    REQUIRED_GATEWAY_CAPABILITIES,
    NativeWorkerControl,
)
from heartwood.gateway.worker_deployment import KubernetesWorkerDeployment
from heartwood.gateway.worker_kubernetes import KubernetesWorkerVolumes
from heartwood.gateway.worker_native import native_worker_plan_factory
from packages.gateway.tests import test_worker_native
from packages.gateway.tests.test_worker_native import _Native, _WorkerApiServer

deployment = test_worker_native.deployment
kubernetes_deployment = test_worker_native.kubernetes_deployment
worker_api = test_worker_native.worker_api
native = test_worker_native.native

_CAPABILITIES = ("openshell.compute.contract", *sorted(REQUIRED_GATEWAY_CAPABILITIES))
_CONFLICT = "delete sandbox failed: Sandbox resource changed during deletion; retry deletion"


class _Kubernetes(_Native):
    """A prepared attempt whose sandbox gateway, cluster, forwarder and API are local fakes."""

    def __init__(
        self,
        settings: KubernetesWorkerDeployment,
        project: ProjectContext,
        monkeypatch: pytest.MonkeyPatch,
        endpoint: str,
    ) -> None:
        self.cluster = "9" * 64
        self.events: list[str] = []
        self.removed: list[str | None] = []
        self.pauses: list[float] = []
        self.failure: dict[str, WorkerAttemptError] = {}
        monkeypatch.setattr(worker_native, "cluster_sha256", lambda _settings: self.cluster)
        monkeypatch.setattr(worker_native, "sleep", self.pauses.append)
        monkeypatch.setattr(
            KubernetesWorkerVolumes, "stage", lambda _volumes, sources: self._staged(sources)
        )
        monkeypatch.setattr(
            KubernetesWorkerVolumes,
            "inspect_worker",
            lambda _volumes, sandbox_id: self._event("inspect", sandbox_id),
        )
        monkeypatch.setattr(
            KubernetesWorkerVolumes,
            "remove_after_revocation",
            lambda _volumes, sandbox_id=None: self._remove(sandbox_id),
        )
        super().__init__(settings, project, monkeypatch, endpoint)  # type: ignore[arg-type]
        self.kubernetes = settings
        self.sandboxes.drivers = [("openshell/kubernetes", _CAPABILITIES)]
        create = self.sandboxes.create

        def created(**request: Any) -> SimpleNamespace:  # noqa: ANN401
            self._event("create")
            return create(**request)

        monkeypatch.setattr(self.sandboxes, "create", created)

    def _event(self, name: str, detail: str | None = None) -> None:
        self.events.append(name if detail is None else f"{name}:{detail}")
        if name in self.failure:
            raise self.failure[name]

    def _staged(self, sources: dict[str, Path]) -> None:
        assert set(sources) == {"inputs", "skills"}
        self._event("stage")

    def _remove(self, sandbox_id: str | None) -> None:
        self._event("remove")
        self.removed.append(sandbox_id)

    def reports(self, *capabilities: str) -> None:
        self.sandboxes.drivers = [("openshell/kubernetes", (*_CAPABILITIES, *capabilities))]

    def error(self, status: str, details: str = "") -> Exception:
        error = self.sandboxes.error(getattr(self.sandboxes.grpc.StatusCode, status))
        error.details = lambda: details  # type: ignore[attr-defined]
        return error

    def answer_deletions(
        self, monkeypatch: pytest.MonkeyPatch, *answers: object
    ) -> list[dict[str, Any]]:
        """Answer each deletion in turn with an error, or by deleting the sandbox."""
        pending = list(answers)
        requests: list[dict[str, Any]] = []
        delete = self.sandboxes.delete

        def scripted(name: str, *, workspace: str, **options: Any) -> object:  # noqa: ANN401
            requests.append(options)
            answer = pending.pop(0) if pending else None
            if isinstance(answer, Exception):
                raise answer
            if answer is not None:
                return answer
            sandbox = self.sandboxes.sandbox
            delete(name, workspace=workspace)
            return SimpleNamespace(outcome=_outcome("COMPLETED"), sandbox_id=sandbox.id)  # type: ignore[union-attr]

        monkeypatch.setattr(self.sandboxes, "delete", scripted)
        return requests


def _outcome(name: str) -> object:
    return getattr(importlib.import_module("openshell").DeletionOutcome, name)


def _as_dict(message: object) -> dict[str, Any]:
    json_format = importlib.import_module("google.protobuf.json_format")
    return cast(dict[str, Any], json_format.MessageToDict(message))


def _writable_workspace(settings: KubernetesWorkerDeployment) -> KubernetesWorkerDeployment:
    policy = json.loads(settings.policy_path.read_text())
    policy["filesystem"]["read_write"] = ["/tmp", "/workspace"]
    settings.policy_path.write_text(json.dumps(policy))
    return settings.model_copy(
        update={"policy_file_sha256": hashlib.sha256(settings.policy_path.read_bytes()).hexdigest()}
    )


@pytest.fixture
def kubernetes(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
    worker_api: _WorkerApiServer,
) -> _Kubernetes:
    settings, project = kubernetes_deployment
    return _Kubernetes(
        _writable_workspace(settings),
        project,
        monkeypatch,
        f"http://127.0.0.1:{worker_api.server_address[1]}/",
    )


def test_launch_stages_creates_and_admits_through_the_kubernetes_driver(
    kubernetes: _Kubernetes,
) -> None:
    assert kubernetes.attempt.plan().control_plane.container_runtime_sha256 == kubernetes.cluster
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    assert runtime.sandbox_id == "sandbox-1"
    assert kubernetes.events == ["stage", "create", "inspect:sandbox-1"]
    spec = kubernetes.sandboxes.spec
    settings = kubernetes.kubernetes
    assert spec.template.image == f"registry.test/heartwood/worker@{settings.image_digest}"
    attempt = kubernetes.intent.attempt_id
    assert dict(spec.template.labels) == {
        "heartwood.project": str(kubernetes.intent.project_id),
        "heartwood.attempt": str(attempt),
        "heartwood.role": "worker",
    }
    assert _as_dict(spec.template.resources) == {
        "limits": {"cpu": settings.cpu, "memory": settings.memory}
    }
    assert _as_dict(spec.template.driver_config) == {
        "kubernetes": {
            "volumes": [
                {
                    "name": mount,
                    "persistent_volume_claim": {
                        "claim_name": f"hw-attempt-{attempt.hex}-{mount}",
                        "read_only": True,
                    },
                }
                for mount in ("inputs", "skills")
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
    }
    assert "/workspace" in spec.policy.filesystem.read_write
    lease = WorkerAttemptLease(kubernetes.record("active", runtime), owns_writer=lambda: True)
    assert kubernetes.attempt.connect(lease).workspace.working_dir == "/workspace"
    evidence = kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert evidence.outcome == "revoked"
    assert kubernetes.removed == ["sandbox-1"]
    assert not kubernetes.sandboxes.running


def test_policy_without_a_writable_working_directory_is_refused_for_kubernetes_only(
    deployment: tuple[Any, ProjectContext],
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, project = kubernetes_deployment
    monkeypatch.setattr(worker_native, "cluster_sha256", lambda _settings: "9" * 64)
    with pytest.raises(WorkerAttemptError, match="native policy does not match"):
        NativeWorkerControl(settings, project).policy()
    with pytest.raises(ProjectStateError, match="unavailable or unsupported"):
        native_worker_plan_factory(settings, project)
    NativeWorkerControl(deployment[0], project).policy()
    NativeWorkerControl(_writable_workspace(settings), project).policy()
    assert not (settings.native_config_home.parent / "kubectl-used").exists()


@pytest.mark.parametrize(("failing", "created"), [("stage", False), ("inspect", True)])
def test_failed_staging_or_admission_never_binds_a_worker(
    kubernetes: _Kubernetes, failing: str, created: bool
) -> None:
    kubernetes.failure[failing] = WorkerAttemptError("synthetic refusal")
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        kubernetes.attempt.launch(kubernetes.intent)
    assert ("create" in kubernetes.events) is created
    assert not kubernetes.sandboxes.running
    assert kubernetes.forwards == []
    # Claims and any staging pod stay until revocation observed the worker's absence.
    assert kubernetes.removed == []
    evidence = kubernetes.attempt.revoke(kubernetes.record("fenced", None))
    assert evidence.outcome == ("revoked" if created else "not-created")
    assert kubernetes.removed == ["sandbox-1" if created else None]


def test_launch_refuses_an_intent_for_another_image_before_staging(
    kubernetes: _Kubernetes,
) -> None:
    intent = kubernetes.intent.model_copy(update={"image_digest": "sha256:" + "e" * 64})
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        kubernetes.attempt.launch(intent)
    assert kubernetes.events == []
    assert kubernetes.sandboxes.spec is None


def test_the_docker_driver_refuses_an_intent_for_another_image_before_staging(
    native: _Native,
) -> None:
    intent = native.intent.model_copy(update={"image_digest": "sha256:" + "e" * 64})
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(intent)
    assert native.sandboxes.spec is None
    assert native.docker.volumes == {}


def test_launch_refuses_a_cluster_that_changed_after_preparation(kubernetes: _Kubernetes) -> None:
    kubernetes.cluster = "8" * 64
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        kubernetes.attempt.launch(kubernetes.intent)
    assert kubernetes.events == []
    assert kubernetes.sandboxes.spec is None


def test_reconciliation_observes_only_the_cluster_and_namespace_that_ran_the_attempt(
    kubernetes: _Kubernetes,
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    reconcile = worker_native.native_worker_reconciler(kubernetes.kubernetes, kubernetes.project)
    kubernetes.cluster = "8" * 64
    with pytest.raises(WorkerControlPlaneError, match="another sandbox control plane"):
        reconcile(kubernetes.record("fenced", runtime))
    assert kubernetes.sandboxes.running
    assert kubernetes.removed == []
    kubernetes.cluster = "9" * 64
    # A manifest that names a newer image still revokes the attempt it retained.
    updated = kubernetes.kubernetes.model_copy(update={"image_digest": "sha256:" + "e" * 64})
    evidence = worker_native.native_worker_reconciler(updated, kubernetes.project)(
        kubernetes.record("fenced", runtime)
    )
    assert evidence.outcome == "revoked"
    assert kubernetes.removed == ["sandbox-1"]


def test_revocation_never_reports_a_worker_the_cluster_still_shows(
    kubernetes: _Kubernetes,
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    kubernetes.failure["remove"] = WorkerAttemptError(
        "native worker remains after control-plane revocation"
    )
    for revoke in (
        kubernetes.attempt.revoke,
        worker_native.native_worker_reconciler(kubernetes.kubernetes, kubernetes.project),
    ):
        with pytest.raises(WorkerAttemptError, match="remains after"):
            revoke(kubernetes.record("fenced", runtime))
    assert kubernetes.removed == []


def test_the_owner_never_reads_absence_in_a_cluster_other_than_the_launch_cluster(
    kubernetes: _Kubernetes,
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    kubernetes.cluster = "8" * 64
    with pytest.raises(WorkerControlPlaneError, match="changed after the launch"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert kubernetes.removed == []
    kubernetes.cluster = "9" * 64
    assert kubernetes.attempt.revoke(kubernetes.record("fenced", runtime)).outcome == "revoked"
    assert kubernetes.removed == ["sandbox-1"]


def test_only_a_driver_failure_at_the_start_of_the_answer_is_retried(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    requests = kubernetes.answer_deletions(
        monkeypatch, kubernetes.error("INTERNAL", "store failed after " + _CONFLICT)
    )
    with pytest.raises(WorkerAttemptError, match="deletion requires reconciliation"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert len(requests) == 1


@pytest.mark.parametrize("revocation", ["owner", "reconciliation"])
def test_a_conflicting_deletion_is_sent_again_until_the_gateway_completes_it(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    requests = kubernetes.answer_deletions(
        monkeypatch,
        kubernetes.error("INTERNAL", _CONFLICT),
        kubernetes.error("FAILED_PRECONDITION", "previous driver operation is still pending"),
        kubernetes.error("UNAVAILABLE"),
    )
    revoke = (
        kubernetes.attempt.revoke
        if revocation == "owner"
        else worker_native.native_worker_reconciler(kubernetes.kubernetes, kubernetes.project)
    )
    evidence = revoke(kubernetes.record("fenced", runtime))
    assert evidence.outcome == "revoked"
    assert evidence.sandbox_id == "sandbox-1"
    assert len(requests) == 4
    assert all(0 < request["timeout_seconds"] <= 90 for request in requests)
    assert kubernetes.pauses == [0.5, 1, 2]
    assert kubernetes.removed == ["sandbox-1"]
    assert not kubernetes.sandboxes.running


def test_a_deletion_that_keeps_conflicting_leaves_the_attempt_fenced(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    requests = kubernetes.answer_deletions(
        monkeypatch, *(kubernetes.error("INTERNAL", _CONFLICT) for _ in range(7))
    )
    with pytest.raises(WorkerAttemptError, match="deletion requires reconciliation"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert len(requests) == 7
    assert kubernetes.pauses == [0.5, 1, 2, 4, 8, 16]
    # The pauses leave most of the deletion's 90 seconds to the requests themselves.
    assert sum(kubernetes.pauses) < 45
    assert kubernetes.sandboxes.running
    assert kubernetes.removed == []
    # A later reconciliation runs the same deletion and completes it.
    reconcile = worker_native.native_worker_reconciler(kubernetes.kubernetes, kubernetes.project)
    assert reconcile(kubernetes.record("fenced", runtime)).outcome == "revoked"
    assert kubernetes.removed == ["sandbox-1"]


def test_retries_stop_at_the_deletion_time_limit(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    requests = kubernetes.answer_deletions(
        monkeypatch, *(kubernetes.error("INTERNAL", _CONFLICT) for _ in range(5))
    )
    clock = iter(range(0, 10_000, 44))
    monkeypatch.setattr(worker_native, "monotonic", lambda: next(clock))
    with pytest.raises(WorkerAttemptError, match="deletion requires reconciliation"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert len(requests) == 2
    assert kubernetes.sandboxes.running


@pytest.mark.parametrize(
    ("status", "details"),
    [
        ("INTERNAL", "fetch sandbox failed: store unavailable"),
        ("INTERNAL", "compute resource was absent, but gateway cleanup did not complete"),
        ("PERMISSION_DENIED", ""),
        ("ABORTED", "sandbox changed"),
        ("DEADLINE_EXCEEDED", ""),
        ("INVALID_ARGUMENT", "sandbox is required"),
    ],
)
def test_a_deletion_failure_that_does_not_ask_for_a_retry_is_not_repeated(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch, status: str, details: str
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    requests = kubernetes.answer_deletions(monkeypatch, kubernetes.error(status, details))
    with pytest.raises(WorkerAttemptError, match="deletion requires reconciliation"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert len(requests) == 1
    assert kubernetes.pauses == []
    assert kubernetes.removed == []


def test_a_retry_never_deletes_a_sandbox_that_replaced_the_attempts(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    requests = kubernetes.answer_deletions(monkeypatch, kubernetes.error("INTERNAL", _CONFLICT))
    monkeypatch.setattr(
        worker_native,
        "sleep",
        lambda _seconds: setattr(kubernetes.sandboxes.sandbox, "id", "replacement"),
    )
    with pytest.raises(WorkerAttemptError, match="identity changed"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert len(requests) == 1
    assert kubernetes.sandboxes.running


@pytest.mark.parametrize("gone", ["during-retry", "at-deletion"])
def test_a_sandbox_another_deletion_removed_still_needs_the_clusters_absence(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    if gone == "during-retry":
        requests = kubernetes.answer_deletions(monkeypatch, kubernetes.error("INTERNAL", _CONFLICT))
        monkeypatch.setattr(
            worker_native, "sleep", lambda _seconds: setattr(kubernetes.sandboxes, "sandbox", None)
        )
    else:

        def vanish(*_arguments: object, **_options: object) -> None:
            kubernetes.sandboxes.sandbox = None
            raise kubernetes.error("NOT_FOUND", "sandbox not found")

        requests = [{}]
        monkeypatch.setattr(kubernetes.sandboxes, "delete", vanish)
    evidence = kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert evidence.outcome == "revoked"
    assert len(requests) == 1
    assert kubernetes.removed == ["sandbox-1"]


def test_the_docker_driver_sends_one_deletion_as_before(
    native: _Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = native.attempt.launch(native.intent)
    assert "docker" in _as_dict(native.sandboxes.spec.template.driver_config)
    assert native.sandboxes.spec.template.image == native.settings.image_digest
    requests: list[dict[str, Any]] = []

    def conflict(_name: str, **options: object) -> None:
        requests.append(options)
        error = native.sandboxes.error(native.sandboxes.grpc.StatusCode.INTERNAL)
        error.details = lambda: _CONFLICT  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(native.sandboxes, "delete", conflict)
    with pytest.raises(native.sandboxes.grpc.RpcError):
        native.attempt.revoke(native.record("fenced", runtime))
    assert requests == [{"workspace": native.settings.workspace, "timeout_seconds": 90}]


def _lost_creation(kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch) -> None:
    def lost(**_request: object) -> SimpleNamespace:
        raise TimeoutError("synthetic creation timeout")

    monkeypatch.setattr(kubernetes.sandboxes, "create", lost)
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        kubernetes.attempt.launch(kubernetes.intent)


@pytest.mark.parametrize("revocation", ["owner", "reconciliation"])
def test_an_unresolved_creation_is_not_created_only_when_the_gateway_confirms_absence(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    _lost_creation(kubernetes, monkeypatch)
    absent = SimpleNamespace(outcome=_outcome("ALREADY_ABSENT"), sandbox_id=None)
    requests = kubernetes.answer_deletions(monkeypatch, absent, absent, absent)

    def revoke() -> Any:  # noqa: ANN401
        record = kubernetes.record("fenced", None)
        if revocation == "owner":
            return kubernetes.attempt.revoke(record)
        return worker_native.native_worker_reconciler(kubernetes.kubernetes, kubernetes.project)(
            record
        )

    # A gateway that does not reject a creation after confirming absence gives no evidence.
    with pytest.raises(WorkerAttemptError, match="absence after an unresolved creation"):
        revoke()
    assert requests == []
    assert kubernetes.removed == []
    if revocation == "owner":
        # The connected client keeps the capabilities it read at launch.
        return
    kubernetes.reports(ABSENCE_REJECTS_CREATION)
    evidence = revoke()
    assert evidence.outcome == "not-created"
    assert evidence.sandbox_id is None
    assert requests == [{"allow_missing": True, "timeout_seconds": 90}]
    assert kubernetes.removed == [None]


@pytest.mark.parametrize(
    "answer",
    ["UNSPECIFIED", "COMPLETED-without-identity", "NOT_FOUND", "INTERNAL", "UNAVAILABLE"],
)
def test_any_other_answer_to_the_absence_question_keeps_the_attempt_fenced(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    kubernetes.reports(ABSENCE_REJECTS_CREATION)
    _lost_creation(kubernetes, monkeypatch)
    if answer == "UNSPECIFIED":
        scripted: object = SimpleNamespace(outcome=_outcome("UNSPECIFIED"), sandbox_id=None)
    elif answer == "COMPLETED-without-identity":
        scripted = SimpleNamespace(outcome=_outcome("COMPLETED"), sandbox_id=None)
    else:
        scripted = kubernetes.error(answer)
    requests = kubernetes.answer_deletions(monkeypatch, scripted)
    with pytest.raises(WorkerAttemptError, match="absence after an unresolved creation"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", None))
    assert len(requests) == 1
    assert kubernetes.removed == []


def test_a_creation_that_arrived_before_the_absence_question_is_recorded_as_revoked(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubernetes.reports(ABSENCE_REJECTS_CREATION)
    _lost_creation(kubernetes, monkeypatch)
    removed = SimpleNamespace(outcome=_outcome("COMPLETED"), sandbox_id="sandbox-late")
    requests = kubernetes.answer_deletions(
        monkeypatch, removed, kubernetes.error("NOT_FOUND", "sandbox not found")
    )
    evidence = kubernetes.attempt.revoke(kubernetes.record("fenced", None))
    assert evidence.outcome == "revoked"
    assert evidence.sandbox_id == "sandbox-late"
    assert requests[0] == {"allow_missing": True, "timeout_seconds": 90}
    assert len(requests) == 2
    assert kubernetes.removed == ["sandbox-late"]


def test_the_owner_resolves_an_unresolved_creation_with_the_capability_read_at_launch(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubernetes.reports(ABSENCE_REJECTS_CREATION)
    _lost_creation(kubernetes, monkeypatch)
    absent = SimpleNamespace(outcome=_outcome("ALREADY_ABSENT"), sandbox_id=None)
    requests = kubernetes.answer_deletions(monkeypatch, absent)
    evidence = kubernetes.attempt.revoke(kubernetes.record("fenced", None))
    assert evidence.outcome == "not-created"
    assert requests == [{"allow_missing": True, "timeout_seconds": 90}]
    assert kubernetes.removed == [None]


def test_an_accepted_deletion_of_an_unresolved_creation_is_followed_until_absence(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubernetes.reports(ABSENCE_REJECTS_CREATION)
    _lost_creation(kubernetes, monkeypatch)
    accepted = SimpleNamespace(outcome=_outcome("ACCEPTED"), sandbox_id="sandbox-late")
    completed = SimpleNamespace(outcome=_outcome("COMPLETED"), sandbox_id="sandbox-late")
    requests = kubernetes.answer_deletions(monkeypatch, accepted, completed)
    evidence = kubernetes.attempt.revoke(kubernetes.record("fenced", None))
    assert (evidence.outcome, evidence.sandbox_id) == ("revoked", "sandbox-late")
    # The accepted deletion is sent again as an ordinary deletion of that sandbox.
    assert [set(request) for request in requests] == [
        {"allow_missing", "timeout_seconds"},
        {"timeout_seconds"},
    ]
    assert kubernetes.removed == ["sandbox-late"]


def test_a_sandbox_that_cannot_be_read_between_retries_keeps_the_attempt_fenced(
    kubernetes: _Kubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = kubernetes.attempt.launch(kubernetes.intent)
    requests = kubernetes.answer_deletions(monkeypatch, kubernetes.error("INTERNAL", _CONFLICT))

    def unavailable(*_arguments: object, **_options: object) -> None:
        raise kubernetes.error("UNAVAILABLE")

    monkeypatch.setattr(
        worker_native,
        "sleep",
        lambda _seconds: monkeypatch.setattr(kubernetes.sandboxes, "get", unavailable),
    )
    with pytest.raises(WorkerAttemptError, match="absence was not observed"):
        kubernetes.attempt.revoke(kubernetes.record("fenced", runtime))
    assert len(requests) == 1
    assert kubernetes.removed == []


def test_the_docker_driver_keeps_an_unresolved_creation_fenced_with_the_same_capability(
    native: _Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native.sandboxes.drivers = [("openshell/docker", (*_CAPABILITIES, ABSENCE_REJECTS_CREATION))]
    deleted: list[object] = []
    monkeypatch.setattr(
        native.sandboxes, "create", lambda **_request: (_ for _ in ()).throw(TimeoutError())
    )
    monkeypatch.setattr(native.sandboxes, "delete", lambda *a, **k: deleted.append((a, k)))
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    for revoke in (
        native.attempt.revoke,
        worker_native.native_worker_reconciler(native.settings, native.project),
    ):
        with pytest.raises(WorkerAttemptError, match="absence after an unresolved creation"):
            revoke(native.record("fenced", None))
    assert deleted == []
