# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native deployment authority and outside lifecycle checks fail closed."""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from openhands.sdk.tool.client_tool import ClientToolSpec

from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptRecord,
)
from heartwood.gateway import ProjectContext, ProjectStateError
from heartwood.gateway._deployment import validate_worker_deployment
from heartwood.gateway.worker_control import NativeWorkerControl
from heartwood.gateway.worker_deployment import OpenShellWorkerDeployment
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.gateway.worker_native import _NativeWorkerAttempt, native_worker_plan_factory
from heartwood.gateway.worker_sessions import WorkerSessionRequest


@pytest.fixture
def deployment() -> Iterator[tuple[OpenShellWorkerDeployment, ProjectContext]]:
    with TemporaryDirectory(prefix="worker-config-", dir=Path("/tmp").resolve()) as temporary:
        root = Path(temporary)
        research = root / "research"
        research.mkdir()
        identity = uuid4()
        project = ProjectContext(
            research, protected_state=root / "protected" / str(identity), project_id=identity
        )
        home = root / "native"
        directory = home / "openshell/gateways/synthetic"
        (directory / "mtls").mkdir(parents=True, mode=0o700)
        (directory / "metadata.json").write_text(
            json.dumps(
                {
                    "name": "synthetic",
                    "auth_mode": "mtls",
                    "gateway_endpoint": "https://127.0.0.1:19670",
                }
            )
        )
        for name in ("ca.crt", "tls.crt", "tls.key"):
            path = directory / "mtls" / name
            path.write_text("synthetic fixture, not key material")
            path.chmod(0o600)
        cli = root / "native-tool"
        cli.write_text("#!/bin/sh\nexit 1\n")
        cli.chmod(0o500)
        policy = root / "policy.json"
        policy.write_text("{}")
        policy.chmod(0o600)
        with socket.socket(socket.AF_UNIX) as daemon:
            daemon.bind(str(root / "docker.sock"))
            settings = OpenShellWorkerDeployment(
                driver="openshell-docker",
                gateway="synthetic",
                native_config_home=home,
                openshell_cli=cli,
                docker_cli=cli,
                docker_host="unix://" + str(root / "docker.sock"),
                workspace="synthetic",
                image_digest="sha256:" + "a" * 64,
                sandbox_policy_sha256="b" * 64,
                policy_path=policy,
                policy_file_sha256=hashlib.sha256(policy.read_bytes()).hexdigest(),
                profile_id="synthetic",
                model="synthetic",
                model_endpoint="https://provider.test/v1/chat/completions",
                provider="synthetic",
                placeholder_env="SYNTHETIC_PLACEHOLDER",
                inputs=WorkerInputManifest(),
                cpu="1",
                memory="2Gi",
            )
            yield settings, project


def test_native_authority_remains_outside_project_and_state(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    settings, project = deployment
    validate_worker_deployment(settings, project)
    assert not project.state_root.exists()
    assert not (project.root / ".heartwood").exists()


def test_group_writable_socket_requires_exact_operator_trust(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    settings, project = deployment
    path = settings.native_config_home.parent / "docker.sock"
    path.chmod(0o660)
    with pytest.raises(ProjectStateError, match="operator-owned local socket"):
        validate_worker_deployment(settings, project)
    group = path.stat().st_gid
    trusted = settings.model_copy(update={"docker_socket_group": group})
    validate_worker_deployment(trusted, project)
    with pytest.raises(ProjectStateError, match="operator-owned local socket"):
        validate_worker_deployment(
            settings.model_copy(update={"docker_socket_group": group + 1}), project
        )
    path.chmod(0o666)
    with pytest.raises(ProjectStateError, match="operator-owned local socket"):
        validate_worker_deployment(trusted, project)


@pytest.mark.parametrize(
    "change",
    [
        "policy",
        "key_permissions",
        "binary_permissions",
        "binary_parent",
        "socket_permissions",
        "socket_file",
        "workspace_authority",
        "hardlink",
    ],
)
def test_changed_or_untrusted_native_authority_is_rejected(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    change: str,
) -> None:
    settings, project = deployment
    if change == "policy":
        settings.policy_path.write_text('{"changed":true}')
    elif change == "key_permissions":
        (settings.native_config_home / "openshell/gateways/synthetic/mtls/tls.key").chmod(0o644)
    elif change == "binary_permissions":
        settings.docker_cli.chmod(0o777)
    elif change == "binary_parent":
        settings.docker_cli.parent.chmod(0o777)
    elif change == "socket_permissions":
        (settings.native_config_home.parent / "docker.sock").chmod(0o777)
    elif change == "socket_file":
        settings = settings.model_copy(
            update={"docker_host": "unix://" + str(settings.policy_path)}
        )
    elif change == "workspace_authority":
        target = project.root / "policy.json"
        target.write_bytes(settings.policy_path.read_bytes())
        settings = settings.model_copy(update={"policy_path": target})
    else:
        (settings.policy_path.parent / "policy-alias").hardlink_to(settings.policy_path)
    with pytest.raises(ProjectStateError):
        validate_worker_deployment(settings, project)
    assert not project.state_root.exists()


def test_unavailable_native_runtime_never_returns_a_local_factory(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, project = deployment
    control = Mock()
    monkeypatch.setattr("heartwood.gateway.worker_native.NativeWorkerControl", control)
    monkeypatch.setattr(
        "heartwood.gateway.worker_native.require_worker_event_hooks",
        Mock(side_effect=RuntimeError("unsupported")),
    )
    with pytest.raises(ProjectStateError, match="unsupported"):
        native_worker_plan_factory(settings, project)
    control.assert_not_called()


def test_native_connection_identity_cannot_change_after_preparation(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, project = deployment
    sdk = SimpleNamespace(
        SandboxStatusRef=SimpleNamespace(__dataclass_fields__={"configuration_admission": None})
    )
    monkeypatch.setattr(
        "heartwood.gateway.worker_control.importlib.import_module", Mock(return_value=sdk)
    )
    control = NativeWorkerControl(settings, project)
    path = settings.native_config_home / "openshell/gateways/synthetic/metadata.json"
    metadata = json.loads(path.read_text())
    metadata["gateway_endpoint"] = "https://replacement.test"
    path.write_text(json.dumps(metadata))
    with pytest.raises(WorkerAttemptError, match="identity changed"):
        control.connect()
    assert control.client is None


def _attempt(settings: OpenShellWorkerDeployment, project: ProjectContext) -> _NativeWorkerAttempt:
    attempt = _NativeWorkerAttempt.__new__(_NativeWorkerAttempt)
    attempt.settings = settings
    attempt._client_tools = ()
    assert project.project_id is not None
    intent = WorkerAttemptIntent(
        project_id=project.project_id,
        session_id="synthetic",
        attempt_id=uuid4(),
        conversation_id=uuid4(),
        image_digest=settings.image_digest,
        gateway_policy_sha256="c" * 64,
        sandbox_policy_sha256=settings.sandbox_policy_sha256,
        input_manifest_sha256="d" * 64,
    )
    attempt._intent = intent
    attempt._preparation = ExitStack()
    attempt._sandbox_id = "native-worker"
    attempt._generation = "generation-1"
    attempt._workspace = None
    attempt._volumes = Mock(
        labels={"heartwood.attempt": str(intent.attempt_id)}, worker_name="unique-attempt"
    )
    attempt._forward = Mock(active=True)
    attempt.control = Mock(
        pb=SimpleNamespace(SANDBOX_PHASE_READY=2, CONFIGURATION_ADMISSION_STATE_ACCEPTED=3)
    )
    attempt.control.client.get.return_value = SimpleNamespace(
        id="native-worker",
        labels=attempt._volumes.labels,
        phase=2,
        status=SimpleNamespace(
            main_process_instance_id="generation-1",
            configuration_activated=True,
            configuration_admission=SimpleNamespace(
                state=3, policy_hash=settings.sandbox_policy_sha256
            ),
        ),
    )
    return attempt


@pytest.mark.parametrize(
    "change", ["identity", "owner", "phase", "generation", "activation", "admission", "policy"]
)
def test_native_admission_rejects_replaced_or_unadmitted_runtime(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    change: str,
) -> None:
    attempt = _attempt(*deployment)
    observed = attempt.control.client.get.return_value
    attempt._admit_native(observed)
    if change == "identity":
        observed.id = "replacement"
    elif change == "owner":
        observed.labels = {}
    elif change == "phase":
        observed.phase = 4
    elif change == "generation":
        observed.status.main_process_instance_id = "replacement"
    elif change == "activation":
        observed.status.configuration_activated = False
    elif change == "admission":
        observed.status.configuration_admission.state = 1
    else:
        observed.status.configuration_admission.policy_hash = "e" * 64
    with pytest.raises(WorkerAttemptError, match="admission changed"):
        attempt._admit_native(observed)


def test_revocation_refuses_replacement_without_deleting_it(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    attempt = _attempt(*deployment)
    attempt.control.client.get.return_value.id = "replacement"
    assert attempt._intent is not None
    record = WorkerAttemptRecord(phase="fenced", intent=attempt._intent)
    with pytest.raises(WorkerAttemptError, match="identity changed"):
        attempt.revoke(record)
    attempt.control.client.delete.assert_not_called()
    cast(Mock, attempt._volumes).remove_after_revocation.assert_not_called()


def test_uncertain_creation_is_revoked_using_outside_identity(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    attempt = _attempt(*deployment)
    attempt._sandbox_id = None
    assert attempt._intent is not None
    record = WorkerAttemptRecord(phase="fenced", intent=attempt._intent)
    evidence = attempt.revoke(record)
    assert evidence.sandbox_id == "native-worker"
    assert evidence.outcome == "revoked"
    attempt.control.client.delete.assert_called_once()
    attempt.control.client.wait_deleted.assert_called_once()
    cast(Mock, attempt._volumes).remove_after_revocation.assert_called_once()


def test_projection_cleanup_failure_never_reports_revocation(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    attempt = _attempt(*deployment)
    cast(Mock, attempt._volumes).remove_after_revocation.side_effect = WorkerAttemptError(
        "worker remains"
    )
    assert attempt._intent is not None
    with pytest.raises(WorkerAttemptError, match="remains"):
        attempt.revoke(WorkerAttemptRecord(phase="fenced", intent=attempt._intent))
    cast(Mock, attempt.control).close.assert_not_called()


def test_prior_process_attempt_cannot_trigger_automatic_revocation(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    attempt = _attempt(*deployment)
    assert attempt._intent is not None
    intent = attempt._intent.model_copy(update={"attempt_id": uuid4()})
    with pytest.raises(WorkerAttemptError, match="this process"):
        attempt.revoke(WorkerAttemptRecord(phase="fenced", intent=intent))
    attempt.control.client.get.assert_not_called()


def test_native_plan_captures_only_trusted_client_tool_specifications(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, project = deployment
    monkeypatch.setattr("heartwood.gateway.worker_native.require_worker_event_hooks", Mock())
    monkeypatch.setattr("heartwood.gateway.worker_native.NativeWorkerControl", Mock())
    attempt = Mock()
    monkeypatch.setattr("heartwood.gateway.worker_native._NativeWorkerAttempt", attempt)
    spec = ClientToolSpec(
        name="propose_record",
        description="Propose a record for outside review.",
        parameters={"type": "object", "properties": {"record": {"type": "string"}}},
    )
    original = spec.model_dump(mode="json")
    factory = native_worker_plan_factory(settings, project, client_tools=(spec,))
    spec.parameters["properties"]["record"]["type"] = "number"
    factory(cast(WorkerSessionRequest, SimpleNamespace(project=project)))
    captured = attempt.call_args.kwargs["client_tools"]
    assert len(captured) == 1
    assert captured[0].model_dump(mode="json") == original
    with pytest.raises(ValueError, match="names must be unique"):
        native_worker_plan_factory(settings, project, client_tools=(spec, spec))
