# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native deployment authority and outside lifecycle checks fail closed."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
import tomli_w
from openhands.sdk.tool.client_tool import ClientToolSpec
from pydantic import ValidationError

from heartwood.core_adapter import worker_attempts
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptLease,
    WorkerAttemptRecord,
    WorkerControlPlane,
    WorkerControlPlaneError,
    WorkerGatewayCapabilityError,
    WorkerGatewayReport,
    WorkerRuntimeBinding,
)
from heartwood.gateway import (
    ProjectContext,
    ProjectStateError,
    load_project_deployment,
    worker_native,
)
from heartwood.gateway._deployment import validate_worker_deployment
from heartwood.gateway.worker_control import (
    REQUIRED_GATEWAY_CAPABILITIES,
    NativeWorkerControl,
    NativeWorkerForward,
)
from heartwood.gateway.worker_deployment import (
    KubernetesWorkerDeployment,
    OpenShellWorkerDeployment,
    WorkerDeployment,
    worker_deployment_from_mapping,
)
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.gateway.worker_native import _NativeWorkerAttempt, native_worker_plan_factory
from heartwood.gateway.worker_sessions import WorkerSessionRequest
from heartwood.gateway.worker_volumes import WorkerVolumes

_GATEWAY_VERSION = "0.0.1+synthetic"
_RUNTIME = "f" * 64
_GATEWAY_CAPABILITIES = ("openshell.compute.contract", *sorted(REQUIRED_GATEWAY_CAPABILITIES))


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
        policy.write_text(
            json.dumps(
                {
                    "landlock": {"compatibility": "hard_requirement"},
                    "process": {"run_as_user": "10001", "run_as_group": "10001"},
                    "filesystem": {"read_only": ["/inputs", "/skills"]},
                }
            )
        )
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
        control_plane=WorkerControlPlane(
            control_plane_sha256="1" * 64, container_runtime_sha256=_RUNTIME
        ),
    )
    attempt._intent = intent
    attempt._creation_uncertain = False
    attempt._preparation = ExitStack()
    attempt._sandbox_id = "native-worker"
    attempt._generation = "generation-1"
    attempt._workspace = None
    attempt._volumes = Mock(
        labels={"heartwood.attempt": str(intent.attempt_id)}, worker_name="unique-attempt"
    )
    attempt._forward = Mock(active=True)
    attempt.control = Mock(
        pb=SimpleNamespace(SANDBOX_PHASE_READY=2, CONFIGURATION_ADMISSION_STATE_ACCEPTED=3),
        gateway=None,
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
    factory(cast(WorkerSessionRequest, SimpleNamespace(project=project, client_tools=())))
    captured = attempt.call_args.kwargs["client_tools"]
    assert len(captured) == 1
    assert captured[0].model_dump(mode="json") == original
    # A session's own client tools, such as its MCP tools, follow the launcher's fixed ones.
    session_spec = spec.model_copy(update={"name": "session_tool"})
    factory(
        cast(WorkerSessionRequest, SimpleNamespace(project=project, client_tools=(session_spec,)))
    )
    assert [item.name for item in attempt.call_args.kwargs["client_tools"]] == [
        "propose_record",
        "session_tool",
    ]
    with pytest.raises(WorkerAttemptError, match="unique"):
        factory(cast(WorkerSessionRequest, SimpleNamespace(project=project, client_tools=(spec,))))
    with pytest.raises(ValueError, match="names must be unique"):
        native_worker_plan_factory(settings, project, client_tools=(spec, spec))


_PLACEHOLDER = "openshell:resolve:env:SYNTHETIC_PLACEHOLDER"
_SKILL = b"synthetic skill"
_CATALOG = "f" * 64


class _Sandboxes:
    """The control-plane observations one uniquely named sandbox can return."""

    def __init__(self, settings: WorkerDeployment) -> None:
        self.settings = settings
        self.pb = pytest.importorskip("openshell._proto.openshell_pb2")
        self.grpc = pytest.importorskip("grpc")
        self.spec: Any = None
        self.sandbox: SimpleNamespace | None = None
        self.deleted: list[str] = []
        self.closed = False
        self.probe: dict[str, object] = {
            "files": {
                "inputs": {},
                "skills": {"synthetic/SKILL.md": hashlib.sha256(_SKILL).hexdigest()},
            },
            "uid": 10001,
            "placeholder": _PLACEHOLDER,
            "catalog": _CATALOG,
        }
        self.probe_output: str | None = None
        self.version: str | None = _GATEWAY_VERSION
        self.drivers: list[tuple[str, tuple[str, ...]]] = [
            ("openshell/docker", _GATEWAY_CAPABILITIES)
        ]
        self.after_probe: Callable[[SimpleNamespace], None] = lambda _sandbox: None

    def place(self, name: str, labels: dict[str, str], *, sandbox_id: str = "sandbox-1") -> None:
        self.sandbox = SimpleNamespace(
            id=sandbox_id,
            name=name,
            labels=dict(labels),
            phase=self.pb.SANDBOX_PHASE_READY,
            status=SimpleNamespace(
                main_process_instance_id="generation-1",
                configuration_activated=True,
                configuration_admission=SimpleNamespace(
                    state=self.pb.CONFIGURATION_ADMISSION_STATE_ACCEPTED,
                    policy_hash=self.settings.sandbox_policy_sha256,
                ),
            ),
        )

    def gateway_info(self) -> object:
        if self.version is None:
            raise self.error(self.grpc.StatusCode.UNAVAILABLE)
        return self.pb.GetGatewayInfoResponse(
            gateway_version=self.version,
            extensions=[
                self.pb.NegotiatedExtensionInfo(
                    kind=self.pb.EXTENSION_KIND_COMPUTE_DRIVER,
                    configured_name="docker",
                    implementation_name=name,
                    supported_capabilities=capabilities,
                )
                for name, capabilities in self.drivers
            ],
        )

    def create(
        self, *, workspace: str, name: str, spec: object, labels: dict[str, str]
    ) -> SimpleNamespace:
        assert workspace == self.settings.workspace
        self.spec = spec
        if self.sandbox is None:
            self.place(name, labels)
        assert self.sandbox is not None
        return SimpleNamespace(id=self.sandbox.id)

    def get(self, name: str, *, workspace: str) -> SimpleNamespace:
        assert workspace == self.settings.workspace
        if self.sandbox is None or self.sandbox.name != name:
            raise self.error(self.grpc.StatusCode.NOT_FOUND)
        return self.sandbox

    def error(self, code: object) -> Exception:
        error = self.grpc.RpcError()
        error.code = lambda: code
        return cast(Exception, error)

    @property
    def running(self) -> bool:
        return self.sandbox is not None

    def wait_ready(self, name: str, *, workspace: str, **_options: object) -> SimpleNamespace:
        return self.get(name, workspace=workspace)

    def exec(self, name: str, command: list[str], **_options: object) -> SimpleNamespace:
        assert command[-2:] == [self.settings.placeholder_env, "terra"]
        sandbox = self.get(name, workspace=self.settings.workspace)
        output = json.dumps(self.probe) if self.probe_output is None else self.probe_output
        self.after_probe(sandbox)
        return SimpleNamespace(exit_code=0, stdout=output)

    def delete(self, name: str, *, workspace: str, **_options: object) -> None:
        self.get(name, workspace=workspace)
        self.deleted.append(name)
        self.sandbox = None

    def wait_deleted(self, _name: str, **_options: object) -> None:
        assert self.sandbox is None

    def close(self) -> None:
        self.closed = True


class _Docker:
    """Attempt-owned Docker resources as the native CLI would list them."""

    def __init__(self) -> None:
        self.volumes: dict[str, dict[str, str]] = {}
        self.containers: list[str] = []

    def command(self, *arguments: str) -> bytes:
        if arguments[:2] == ("container", "ls"):
            return " ".join(self.containers).encode()
        if arguments[:2] == ("volume", "ls"):
            name = arguments[-1].removeprefix("name=^").removesuffix("$")
            return name.encode() if name in self.volumes else b""
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps([{"Labels": self.volumes[arguments[2]]}]).encode()
        assert arguments[:2] == ("volume", "rm")
        del self.volumes[arguments[2]]
        return b""


class _Forward:
    def __init__(self, native: _Native) -> None:
        self.native = native
        self.active = False

    def start(self) -> str:
        self.active = True
        return self.native.endpoint

    def close(self) -> None:
        if self.native.forward_stuck:
            raise WorkerAttemptError("native forwarding output did not stop")
        self.active = False


class _Native:
    """A prepared native attempt whose control plane, Docker, forwarder and API are local fakes."""

    def __init__(
        self,
        settings: OpenShellWorkerDeployment,
        project: ProjectContext,
        monkeypatch: pytest.MonkeyPatch,
        endpoint: str,
    ) -> None:
        self.settings = settings
        self.project = project
        self.endpoint = endpoint
        self.sandboxes = _Sandboxes(settings)
        self.docker = _Docker()
        self.forwards: list[_Forward] = []
        self.forward_stuck = False
        self.inspection_error: WorkerAttemptError | None = None
        project.state_root.mkdir(parents=True, mode=0o700)
        monkeypatch.setattr("openshell.SandboxClient", lambda *_args, **_options: self.sandboxes)
        monkeypatch.setattr(
            WorkerVolumes, "command", lambda _volumes, *args: self.docker.command(*args)
        )
        monkeypatch.setattr(
            WorkerVolumes, "stage", lambda volumes, sources: self._stage(volumes, sources)
        )
        monkeypatch.setattr(
            WorkerVolumes, "inspect_worker", lambda volumes, name: self._inspect(volumes, name)
        )
        monkeypatch.setattr(worker_native, "NativeWorkerForward", self._forward)
        self.runtime = _RUNTIME
        monkeypatch.setattr(worker_native, "container_runtime_sha256", lambda _: self.runtime)
        self.attempt = _NativeWorkerAttempt(
            settings,
            cast(
                WorkerSessionRequest,
                SimpleNamespace(
                    project=project,
                    session_id="synthetic",
                    profile=SimpleNamespace(
                        profile_id=settings.profile_id,
                        model=settings.model,
                        policy_endpoint=settings.model_endpoint,
                    ),
                    skills=self,
                    specialists=SimpleNamespace(fingerprint=_CATALOG),
                ),
            ),
        )
        assert project.project_id is not None
        self.intent = WorkerAttemptIntent(
            project_id=project.project_id,
            session_id="synthetic",
            attempt_id=uuid4(),
            conversation_id=uuid4(),
            image_digest=settings.image_digest,
            gateway_policy_sha256="c" * 64,
            sandbox_policy_sha256=settings.sandbox_policy_sha256,
            input_manifest_sha256=self.attempt.plan().input_manifest_sha256,
            control_plane=self.attempt.plan().control_plane,
        )

    platform_id = "terra"

    @contextmanager
    def stage_worker_skills(
        self, *, project_root: Path, staging_parent: Path
    ) -> Iterator[SimpleNamespace]:
        assert project_root == self.project.root
        skill = staging_parent / "skills" / "synthetic"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(_SKILL)
        yield SimpleNamespace(root=skill.parent, sha256="e" * 64, agent_skills=lambda: ())

    def _stage(self, volumes: WorkerVolumes, sources: dict[str, Path]) -> None:
        assert set(sources) == set(volumes.names)
        for name in volumes.names.values():
            self.docker.volumes[name] = {**volumes.labels, "heartwood.role": "projection"}

    def _inspect(self, _volumes: WorkerVolumes, name: str) -> None:
        assert name.endswith("-sandbox-1")
        if self.inspection_error is not None:
            raise self.inspection_error

    def _forward(self, _control: NativeWorkerControl, _name: str) -> _Forward:
        self.forwards.append(_Forward(self))
        return self.forwards[-1]

    def record(self, phase: str, runtime: WorkerRuntimeBinding | None) -> WorkerAttemptRecord:
        return WorkerAttemptRecord.model_validate(
            {"phase": phase, "intent": self.intent, "runtime": runtime}
        )


class _WorkerApiServer(ThreadingHTTPServer):
    open_access = False


class _WorkerApi(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        authenticated = bool(self.headers.get("X-Session-API-Key"))
        open_access = cast(_WorkerApiServer, self.server).open_access
        self.send_response(200 if authenticated or open_access else 401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@pytest.fixture
def worker_api() -> Iterator[_WorkerApiServer]:
    server = _WorkerApiServer(("127.0.0.1", 0), _WorkerApi)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def native(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
    worker_api: _WorkerApiServer,
) -> _Native:
    settings, project = deployment
    return _Native(
        settings, project, monkeypatch, f"http://127.0.0.1:{worker_api.server_address[1]}/"
    )


def test_launch_binds_the_observed_runtime_and_admits_only_the_native_placeholder(
    native: _Native,
) -> None:
    runtime = native.attempt.launch(native.intent)
    assert runtime.sandbox_id == "sandbox-1"
    assert runtime.generation == "generation-1"
    assert str(runtime.endpoint) == native.endpoint
    assert native.sandboxes.deleted == []
    spec = native.sandboxes.spec
    assert list(spec.providers) == [native.settings.provider]
    assert native.settings.placeholder_env not in spec.environment
    assert spec.environment["HEARTWOOD_SKILL_PLATFORM"] == "terra"
    lease = WorkerAttemptLease(native.record("active", runtime), owns_writer=lambda: True)
    context = native.attempt.connect(lease)
    assert context.inference_placeholder.get_secret_value() == _PLACEHOLDER
    assert context.workspace.attempt is lease
    with pytest.raises(WorkerAttemptError, match="cannot be repeated"):
        native.attempt.launch(native.intent)


@pytest.mark.parametrize(
    "field", ["project_id", "session_id", "input_manifest_sha256", "sandbox_policy_sha256"]
)
def test_launch_refuses_an_intent_that_differs_from_preparation(
    native: _Native, field: str
) -> None:
    changed: dict[str, object] = {
        "project_id": uuid4(),
        "session_id": "another-session",
        "input_manifest_sha256": "0" * 64,
        "sandbox_policy_sha256": "0" * 64,
    }
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent.model_copy(update={field: changed[field]}))
    assert native.attempt.control.client is None
    assert native.docker.volumes == {}
    assert native.forwards == []


def _change_admission(native: _Native, change: str) -> None:
    sandboxes = native.sandboxes
    assert native.project.project_id is not None
    name = "hw-" + native.intent.attempt_id.hex[:16]
    labels = {
        "heartwood.project": str(native.project.project_id),
        "heartwood.attempt": str(native.intent.attempt_id),
    }
    sandboxes.place(name, labels)
    assert sandboxes.sandbox is not None
    status = sandboxes.sandbox.status
    if change == "policy":
        status.configuration_admission.policy_hash = "0" * 64
    elif change == "admission":
        status.configuration_admission.state = (
            sandboxes.pb.CONFIGURATION_ADMISSION_STATE_UNSPECIFIED
        )
    elif change == "activation":
        status.configuration_activated = False
    elif change == "generation":
        status.main_process_instance_id = ""
    elif change == "network":
        native.inspection_error = WorkerAttemptError("network does not match admission")
    else:
        assert change == "restarted"

        def restart(sandbox: SimpleNamespace) -> None:
            sandbox.status.main_process_instance_id = "generation-2"

        sandboxes.after_probe = restart


@pytest.mark.parametrize(
    "change", ["policy", "admission", "activation", "generation", "network", "restarted"]
)
def test_worker_that_fails_admission_is_stopped_and_never_bound(
    native: _Native, change: str
) -> None:
    _change_admission(native, change)
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    assert not native.sandboxes.running
    assert native.sandboxes.deleted == ["hw-" + native.intent.attempt_id.hex[:16]]
    assert not any(forward.active for forward in native.forwards)
    assert bool(native.forwards) == (change == "restarted")
    evidence = native.attempt.revoke(native.record("fenced", None))
    assert evidence.outcome == "revoked"
    assert evidence.sandbox_id == "sandbox-1"
    assert native.docker.volumes == {}


def test_lost_creation_and_deletion_responses_are_still_recorded_as_revoked(
    native: _Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    create, delete = native.sandboxes.create, native.sandboxes.delete

    def lost_create(
        *, workspace: str, name: str, spec: object, labels: dict[str, str]
    ) -> SimpleNamespace:
        create(workspace=workspace, name=name, spec=spec, labels=labels)
        raise TimeoutError("synthetic lost response")

    def lost_delete(name: str, *, workspace: str, timeout_seconds: float) -> None:
        delete(name, workspace=workspace, timeout_seconds=timeout_seconds)
        raise TimeoutError("synthetic lost response")

    monkeypatch.setattr(native.sandboxes, "create", lost_create)
    monkeypatch.setattr(native.sandboxes, "delete", lost_delete)
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    assert not native.sandboxes.running
    evidence = native.attempt.revoke(native.record("fenced", None))
    assert evidence.outcome == "revoked"
    assert evidence.sandbox_id == "sandbox-1"
    assert native.docker.volumes == {}


def test_foreign_sandbox_under_the_attempt_name_is_never_deleted(native: _Native) -> None:
    native.sandboxes.place("hw-" + native.intent.attempt_id.hex[:16], {"heartwood.attempt": "x"})
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    assert native.sandboxes.deleted == []
    assert native.sandboxes.running


@pytest.mark.parametrize(
    "change", ["uid", "extra-file", "hash", "raw-key", "catalog", "oversized", "not-json"]
)
def test_probe_mismatch_stops_the_worker_and_retains_no_placeholder(
    native: _Native, change: str
) -> None:
    probe = native.sandboxes.probe
    files = cast(dict[str, dict[str, str]], probe["files"])
    if change == "uid":
        probe["uid"] = 0
    elif change == "extra-file":
        files["inputs"]["unselected.csv"] = "0" * 64
    elif change == "hash":
        files["skills"]["synthetic/SKILL.md"] = "0" * 64
    elif change == "raw-key":
        probe["placeholder"] = "synthetic-provider-key"
    elif change == "catalog":
        probe["catalog"] = "0" * 64
    elif change == "oversized":
        native.sandboxes.probe_output = json.dumps({**probe, "padding": "x" * (1024 * 1024)})
    else:
        native.sandboxes.probe_output = "not json"
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    assert native.forwards == []
    assert not native.sandboxes.running
    lease = cast(WorkerAttemptLease, SimpleNamespace(intent=native.intent))
    with pytest.raises(WorkerAttemptError, match="does not match its launch"):
        native.attempt.connect(lease)


def test_worker_api_that_answers_without_authentication_is_stopped(
    native: _Native, worker_api: _WorkerApiServer
) -> None:
    worker_api.open_access = True
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    assert not native.sandboxes.running
    assert [forward.active for forward in native.forwards] == [False]


@pytest.mark.parametrize("change", ["generation", "policy", "replaced", "forward"])
def test_runtime_observation_fences_a_changed_worker(
    native: _Native, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_attempts, "_RUNTIME_OBSERVATION_SECONDS", 0.0)
    runtime = native.attempt.launch(native.intent)
    lease = WorkerAttemptLease(native.record("active", runtime), owns_writer=lambda: True)
    native.attempt.connect(lease)
    lease.ensure_active()
    sandbox = native.sandboxes.sandbox
    assert sandbox is not None
    if change == "generation":
        sandbox.status.main_process_instance_id = "generation-2"
    elif change == "policy":
        sandbox.status.configuration_admission.policy_hash = "0" * 64
    elif change == "replaced":
        sandbox.id = "sandbox-2"
    else:
        native.forwards[0].active = False
    with pytest.raises(WorkerAttemptError, match="observation closed"):
        lease.ensure_active()
    assert not lease.active


def test_worker_that_fails_its_first_runtime_observation_is_stopped(native: _Native) -> None:
    runtime = native.attempt.launch(native.intent)
    assert native.sandboxes.sandbox is not None
    native.sandboxes.sandbox.status.main_process_instance_id = "generation-2"
    lease = WorkerAttemptLease(native.record("active", runtime), owns_writer=lambda: True)
    with pytest.raises(WorkerAttemptError):
        native.attempt.connect(lease)
    assert not native.sandboxes.running
    assert [forward.active for forward in native.forwards] == [False]
    assert not lease.active


@pytest.mark.parametrize("obstacle", ["forwarder", "rotated-identity"])
def test_revocation_deletes_the_sandbox_before_local_cleanup_can_fail(
    native: _Native, obstacle: str
) -> None:
    runtime = native.attempt.launch(native.intent)
    fenced = native.record("fenced", runtime)
    certificate = native.settings.native_config_home / "openshell/gateways/synthetic/mtls/tls.crt"
    original = certificate.read_text()
    if obstacle == "forwarder":
        native.forward_stuck = True
    else:
        certificate.write_text("rotated synthetic fixture, not key material")
    with pytest.raises(WorkerAttemptError):
        native.attempt.revoke(fenced)
    assert native.sandboxes.deleted == ["hw-" + native.intent.attempt_id.hex[:16]]
    assert len(native.docker.volumes) == 2
    native.forward_stuck = False
    certificate.write_text(original)
    evidence = native.attempt.revoke(fenced)
    assert evidence.outcome == "revoked"
    assert evidence.sandbox_id == runtime.sandbox_id
    assert native.docker.volumes == {}
    assert native.sandboxes.closed


@pytest.mark.parametrize("obstacle", ["control", "replaced"])
def test_forwarder_is_closed_when_the_sandbox_cannot_be_deleted(
    native: _Native, monkeypatch: pytest.MonkeyPatch, obstacle: str
) -> None:
    runtime = native.attempt.launch(native.intent)
    assert native.sandboxes.sandbox is not None
    if obstacle == "replaced":
        native.sandboxes.sandbox.labels["heartwood.attempt"] = str(uuid4())
    else:

        def unavailable(_name: str, **_options: object) -> SimpleNamespace:
            raise native.sandboxes.error(native.sandboxes.grpc.StatusCode.UNAVAILABLE)

        monkeypatch.setattr(native.sandboxes, "get", unavailable)
    with pytest.raises(WorkerAttemptError):
        native.attempt.revoke(native.record("fenced", runtime))
    assert native.sandboxes.deleted == []
    assert [forward.active for forward in native.forwards] == [False]
    assert len(native.docker.volumes) == 2


def test_interrupted_launch_stops_the_sandbox_it_created(native: _Native) -> None:
    def interrupt(_sandbox: SimpleNamespace) -> None:
        raise KeyboardInterrupt

    native.sandboxes.after_probe = interrupt
    with pytest.raises(KeyboardInterrupt):
        native.attempt.launch(native.intent)
    assert not native.sandboxes.running
    assert native.sandboxes.deleted == ["hw-" + native.intent.attempt_id.hex[:16]]


@pytest.mark.parametrize("phase", ["prepared", "active"])
def test_new_process_reconciles_an_attempt_its_owner_left_running(
    native: _Native, phase: str
) -> None:
    runtime = native.attempt.launch(native.intent)
    upgraded = native.settings.model_copy(update={"image_digest": "sha256:" + "9" * 64})
    reconcile = worker_native.native_worker_reconciler(upgraded, native.project)
    fenced = native.record("fenced", runtime if phase == "active" else None)
    with pytest.raises(WorkerAttemptError, match="fenced"):
        reconcile(native.record(phase, runtime if phase == "active" else None))
    assert native.sandboxes.deleted == []
    evidence = reconcile(fenced)
    assert evidence.attempt_id == native.intent.attempt_id
    assert evidence.outcome == "revoked"
    assert evidence.sandbox_id == "sandbox-1"
    assert native.sandboxes.deleted == ["hw-" + native.intent.attempt_id.hex[:16]]
    assert native.docker.volumes == {}
    assert native.sandboxes.closed
    WorkerAttemptRecord.model_validate(
        {
            "phase": "revoked",
            "intent": fenced.intent,
            "runtime": fenced.runtime,
            "revocation": evidence,
        }
    )


def test_reconciliation_never_records_absence_after_a_launch_left_unresolved(
    native: _Native,
) -> None:
    reconcile = worker_native.native_worker_reconciler(native.settings, native.project)
    # The ended process may have sent a creation the control plane has not recorded yet.
    with pytest.raises(WorkerAttemptError, match="absence after an unresolved creation"):
        reconcile(native.record("fenced", None))
    assert native.sandboxes.deleted == []


@pytest.mark.parametrize("obstacle", ["project", "owner", "replaced", "container", "control"])
def test_reconciliation_never_reports_revocation_it_did_not_observe(
    native: _Native, monkeypatch: pytest.MonkeyPatch, obstacle: str
) -> None:
    runtime = native.attempt.launch(native.intent)
    project = native.project
    sandbox = native.sandboxes.sandbox
    assert sandbox is not None
    if obstacle == "project":
        identity = uuid4()
        project = ProjectContext(
            project.root,
            protected_state=project.state_root.parent / str(identity),
            project_id=identity,
        )
    elif obstacle == "owner":
        sandbox.labels["heartwood.attempt"] = str(uuid4())
    elif obstacle == "replaced":
        sandbox.id = "sandbox-2"
    elif obstacle == "container":
        native.docker.containers.append("remaining-worker")
    else:

        def unavailable(_name: str, **_options: object) -> SimpleNamespace:
            raise native.sandboxes.error(native.sandboxes.grpc.StatusCode.UNAVAILABLE)

        monkeypatch.setattr(native.sandboxes, "get", unavailable)
    reconcile = worker_native.native_worker_reconciler(native.settings, project)
    with pytest.raises(WorkerAttemptError):
        reconcile(native.record("fenced", runtime))
    assert len(native.docker.volumes) == 2
    assert native.sandboxes.running == (obstacle != "container")


def _offline_control(
    settings: OpenShellWorkerDeployment, project: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> NativeWorkerControl:
    sdk = SimpleNamespace(
        SandboxStatusRef=SimpleNamespace(__dataclass_fields__={"configuration_admission": None})
    )
    monkeypatch.setattr(
        "heartwood.gateway.worker_control.importlib.import_module", Mock(return_value=sdk)
    )
    return NativeWorkerControl(settings, project)


def _forwarder(settings: OpenShellWorkerDeployment, script: str) -> None:
    settings.openshell_cli.chmod(0o700)
    settings.openshell_cli.write_text("#!/bin/sh\n" + script)
    settings.openshell_cli.chmod(0o500)


def test_forwarder_reports_its_loopback_endpoint_without_ambient_authority(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, project = deployment
    monkeypatch.setenv("SYNTHETIC_SECRET", "not-for-forwarding")
    _forwarder(
        settings,
        'echo "$@" > "$XDG_CONFIG_HOME/arguments"\nenv > "$XDG_CONFIG_HOME/environment"\n'
        'echo "Forwarding 127.0.0.1:43210 -> unique-attempt:8000"\nexec sleep 30\n',
    )
    forward = NativeWorkerForward(
        _offline_control(settings, project, monkeypatch), "unique-attempt"
    )
    observed = [forward.active]
    try:
        assert forward.start() == "http://127.0.0.1:43210/"
        observed.append(forward.active)
        with pytest.raises(WorkerAttemptError, match="cannot be restarted"):
            forward.start()
    finally:
        forward.close()
    observed.append(forward.active)
    assert observed == [False, True, False]
    arguments = (settings.native_config_home / "arguments").read_text().split()
    assert arguments[:3] == ["forward", "service", "unique-attempt"]
    assert arguments[arguments.index("--gateway") + 1] == settings.gateway
    assert arguments[arguments.index("--workspace") + 1] == settings.workspace
    assert arguments[arguments.index("--local") + 1] == "127.0.0.1:0"
    assert "SYNTHETIC_SECRET" not in (settings.native_config_home / "environment").read_text()


def test_forwarder_that_exits_without_an_endpoint_requires_reconciliation(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, project = deployment
    _forwarder(settings, 'echo "Forwarding 0.0.0.0:43210 -> unique-attempt:8000"\nexit 1\n')
    forward = NativeWorkerForward(
        _offline_control(settings, project, monkeypatch), "unique-attempt"
    )
    with pytest.raises(WorkerAttemptError, match="reconciliation"):
        forward.start()
    forward.close()
    assert not forward.active


def test_forwarder_is_not_started_with_a_changed_gateway_identity(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, project = deployment
    _forwarder(settings, 'touch "$XDG_CONFIG_HOME/started"\nexec sleep 30\n')
    control = _offline_control(settings, project, monkeypatch)
    certificate = settings.native_config_home / "openshell/gateways/synthetic/mtls/tls.crt"
    certificate.write_text("rotated synthetic fixture, not key material")
    forward = NativeWorkerForward(control, "unique-attempt")
    with pytest.raises(WorkerAttemptError, match="identity changed"):
        forward.start()
    assert not forward.active
    assert not (settings.native_config_home / "started").exists()


_DELETE_RETRY = "openshell.compute.delete-retries-deleting"
_DELETE_AWAITS_PROVISIONING = "openshell.compute.delete-awaits-provisioning"


_UNREADABLE = WorkerGatewayReport(decision="refused")


def _refused(driver: str | None, *capabilities: str) -> WorkerGatewayReport:
    return WorkerGatewayReport(
        decision="refused", version=_GATEWAY_VERSION, driver=driver, capabilities=capabilities
    )


@pytest.mark.parametrize(
    ("drivers", "recorded"),
    [
        pytest.param(None, _UNREADABLE, id="unreadable"),
        pytest.param([], _refused(None), id="no-driver"),
        pytest.param(
            [("openshell/docker", ())], _refused("openshell/docker"), id="no-capabilities"
        ),
        pytest.param(
            [("openshell/docker", (_DELETE_RETRY,))],
            _refused("openshell/docker", _DELETE_RETRY),
            id="no-provisioning-wait",
        ),
        pytest.param(
            [("openshell/docker", (_DELETE_AWAITS_PROVISIONING,))],
            _refused("openshell/docker", _DELETE_AWAITS_PROVISIONING),
            id="no-retry",
        ),
        pytest.param(
            [("openshell/podman", _GATEWAY_CAPABILITIES)],
            _refused("openshell/podman", *_GATEWAY_CAPABILITIES),
            id="other-driver",
        ),
        pytest.param(
            [("openshell/kubernetes", _GATEWAY_CAPABILITIES)],
            _refused("openshell/kubernetes", *_GATEWAY_CAPABILITIES),
            id="kubernetes-driver",
        ),
        pytest.param(
            [("openshell/docker", _GATEWAY_CAPABILITIES)] * 2, _refused(None), id="ambiguous"
        ),
        pytest.param([("openshell/docker", ("OpenShell.Compute.X",))], _UNREADABLE, id="malformed"),
    ],
)
def test_gateway_without_deletion_capabilities_stops_launch_and_reconciliation(
    native: _Native,
    drivers: list[tuple[str, tuple[str, ...]]] | None,
    recorded: WorkerGatewayReport,
) -> None:
    if drivers is None:
        native.sandboxes.version = None
    else:
        native.sandboxes.drivers = drivers
    with pytest.raises(WorkerGatewayCapabilityError, match="required deletion capabilities"):
        worker_native.require_gateway_capabilities(native.settings, native.project)
    assert native.sandboxes.closed
    # The refusal keeps its diagnostic: nothing was created, so nothing needs reconciliation.
    with pytest.raises(WorkerGatewayCapabilityError, match="required deletion capabilities"):
        native.attempt.launch(native.intent)
    assert native.attempt.control.client is None
    assert native.sandboxes.spec is None
    assert native.docker.volumes == {}
    evidence = native.attempt.revoke(native.record("fenced", None))
    assert evidence.outcome == "not-created"
    assert evidence.gateway == recorded

    name = "hw-" + native.intent.attempt_id.hex[:16]
    native.sandboxes.place(name, {"heartwood.attempt": str(native.intent.attempt_id)})
    reconcile = worker_native.native_worker_reconciler(native.settings, native.project)
    with pytest.raises(WorkerGatewayCapabilityError):
        reconcile(native.record("fenced", None))
    assert native.sandboxes.running
    assert native.sandboxes.deleted == []


def test_missing_capability_diagnostic_names_version_and_capability(native: _Native) -> None:
    native.sandboxes.drivers = [("openshell/docker", (_DELETE_AWAITS_PROVISIONING,))]
    with pytest.raises(WorkerGatewayCapabilityError) as raised:
        worker_native.require_gateway_capabilities(native.settings, native.project)
    assert str(raised.value).endswith(f"version {_GATEWAY_VERSION} lacks {_DELETE_RETRY}")


@pytest.mark.parametrize("version", [_GATEWAY_VERSION, "9.9.9-unlisted"])
def test_reported_capabilities_admit_any_gateway_version_and_are_recorded(
    native: _Native, version: str
) -> None:
    native.sandboxes.version = version
    worker_native.require_gateway_capabilities(native.settings, native.project)
    expected = WorkerGatewayReport(
        decision="accepted",
        version=version,
        driver="openshell/docker",
        capabilities=_GATEWAY_CAPABILITIES,
    )
    runtime = native.attempt.launch(native.intent)
    assert runtime.gateway == expected
    evidence = native.attempt.revoke(native.record("fenced", runtime))
    assert evidence.gateway == expected

    reconciled = worker_native.native_worker_reconciler(native.settings, native.project)(
        native.record("fenced", runtime)
    )
    assert reconciled.gateway == expected


_KUBERNETES_FIELDS = (
    "kubectl_cli",
    "kubeconfig",
    "namespace",
    "supervisor_image_digest",
    "sandbox_image_digest",
)


@pytest.fixture
def kubernetes_deployment(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> tuple[KubernetesWorkerDeployment, ProjectContext]:
    """The same operator files, with a tool that records any use in place of `kubectl`."""
    docker, project = deployment
    root = docker.native_config_home.parent
    kubectl = root / "kubectl"
    kubectl.write_text(f"#!/bin/sh\ntouch {root / 'kubectl-used'}\nexit 1\n")
    kubectl.chmod(0o500)
    kubeconfig = root / "kubeconfig"
    kubeconfig.write_text("synthetic fixture, not a credential")
    kubeconfig.chmod(0o600)
    settings = KubernetesWorkerDeployment.model_validate(
        {
            **docker.model_dump(exclude={"docker_cli", "docker_host", "docker_socket_group"}),
            "driver": "openshell-kubernetes",
            "kubectl_cli": kubectl,
            "kubeconfig": kubeconfig,
            "namespace": "hw-s-synthetic",
            "supervisor_image_digest": "sha256:" + "5" * 64,
            "sandbox_image_digest": "sha256:" + "6" * 64,
        }
    )
    return settings, project


def test_manifest_selects_the_settings_of_its_worker_driver(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
) -> None:
    docker, _project = deployment
    kubernetes, _project = kubernetes_deployment
    assert worker_deployment_from_mapping(docker.model_dump()) == docker
    assert worker_deployment_from_mapping(kubernetes.model_dump()) == kubernetes
    for driver in ("openshell-podman", "", None):
        with pytest.raises(ValidationError):
            worker_deployment_from_mapping({**kubernetes.model_dump(), "driver": driver})
    without_driver = kubernetes.model_dump(exclude={"driver"})
    with pytest.raises(ValidationError):
        worker_deployment_from_mapping(without_driver)


@pytest.mark.parametrize("field", _KUBERNETES_FIELDS)
def test_each_driver_requires_its_fields_and_refuses_the_other_drivers(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    field: str,
) -> None:
    docker = deployment[0].model_dump()
    kubernetes = kubernetes_deployment[0].model_dump()
    with pytest.raises(ValidationError):
        worker_deployment_from_mapping({k: v for k, v in kubernetes.items() if k != field})
    with pytest.raises(ValidationError):
        worker_deployment_from_mapping({**docker, field: kubernetes[field]})
    for name in ("docker_cli", "docker_host", "docker_socket_group"):
        with pytest.raises(ValidationError):
            worker_deployment_from_mapping({**kubernetes, name: docker[name] or 0})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kubectl_cli", "bin/kubectl"),
        ("kubectl_cli", "/opt/../kubectl"),
        ("kubeconfig", "kubeconfig"),
        ("kubeconfig", "/etc/heartwood/../kubeconfig"),
        ("namespace", "Sessions"),
        ("namespace", "a" * 64),
        ("supervisor_image_digest", "ghcr.io/nvidia/openshell/supervisor:0.1.8"),
        ("sandbox_image_digest", "sha256:" + "a" * 63),
        # The sandbox command-line tool accepts at most 19 characters with this driver.
        ("workspace", "a" * 20),
    ],
)
def test_invalid_kubernetes_settings_are_refused(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        worker_deployment_from_mapping({**kubernetes_deployment[0].model_dump(), field: value})


def test_workspace_name_limit_applies_to_the_kubernetes_driver_only(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
) -> None:
    longest = {**kubernetes_deployment[0].model_dump(), "workspace": "a" * 19}
    assert worker_deployment_from_mapping(longest).workspace == "a" * 19
    docker = {**deployment[0].model_dump(), "workspace": "a" * 63}
    assert worker_deployment_from_mapping(docker).workspace == "a" * 63


@pytest.mark.parametrize(
    "change",
    [
        "kubeconfig_permissions",
        "kubeconfig_missing",
        "kubeconfig_authority",
        "kubeconfig_link",
        "kubectl_permissions",
        "kubectl_not_executable",
    ],
)
def test_untrusted_kubernetes_authority_is_rejected(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext], change: str
) -> None:
    settings, project = kubernetes_deployment
    validate_worker_deployment(settings, project)
    if change == "kubeconfig_permissions":
        settings.kubeconfig.chmod(0o640)
    elif change == "kubeconfig_missing":
        settings.kubeconfig.unlink()
    elif change == "kubeconfig_authority":
        target = project.root / "kubeconfig"
        target.write_bytes(settings.kubeconfig.read_bytes())
        target.chmod(0o600)
        settings = settings.model_copy(update={"kubeconfig": target})
    elif change == "kubeconfig_link":
        link = settings.kubeconfig.parent / "kubeconfig-link"
        link.symlink_to(settings.kubeconfig)
        settings = settings.model_copy(update={"kubeconfig": link})
    elif change == "kubectl_permissions":
        settings.kubectl_cli.chmod(0o777)
    else:
        settings.kubectl_cli.chmod(0o400)
    with pytest.raises((ProjectStateError, OSError)):
        validate_worker_deployment(settings, project)
    assert not project.state_root.exists()


def _worker_manifest(settings: WorkerDeployment, project: ProjectContext) -> tuple[Path, Any]:
    record = {
        "schema_version": "heartwood.project-deployment.v1",
        "project_id": str(project.project_id),
        "workspace": str(project.root),
        "state_root": str(project.state_root),
        "policy": {"policy_id": "deployment", "platform_id": "generic"},
        "worker": settings.model_dump(mode="json", exclude_none=True),
    }
    path = settings.native_config_home.parent / "deployment.toml"
    project.state_root.parent.mkdir(mode=0o700)
    return path, record


def test_kubernetes_manifest_loads_and_invalid_tables_stop_with_the_manifest_diagnostic(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
) -> None:
    settings, project = kubernetes_deployment
    path, record = _worker_manifest(settings, project)

    def load(worker: dict[str, object]) -> WorkerDeployment | None:
        path.write_text(tomli_w.dumps({**record, "worker": worker}))
        path.chmod(0o600)
        return load_project_deployment(path, project_root=project.root, env={}).worker

    table = record["worker"]
    assert load(table) == settings
    for invalid in (
        {**table, "docker_cli": table["kubectl_cli"]},
        {**table, "docker_host": "unix:///run/docker.sock"},
        {**table, "driver": "openshell-docker"},
        {**table, "driver": "openshell-podman"},
        {**table, "workspace": "a" * 20},
        {k: v for k, v in table.items() if k != "namespace"},
        {k: v for k, v in table.items() if k != "kubeconfig"},
    ):
        with pytest.raises(ProjectStateError, match="HW-DEPLOY-001"):
            load(invalid)
    settings.kubeconfig.chmod(0o644)
    with pytest.raises(ProjectStateError, match="HW-DEPLOY-001"):
        load(table)
    assert not project.state_root.exists()
    assert not (settings.native_config_home.parent / "kubectl-used").exists()


@pytest.fixture
def kubernetes_gateway(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> _Sandboxes:
    sandboxes = _Sandboxes(kubernetes_deployment[0])
    sandboxes.drivers = [("openshell/kubernetes", _GATEWAY_CAPABILITIES)]
    monkeypatch.setattr("openshell.SandboxClient", lambda *_args, **_options: sandboxes)
    return sandboxes


def test_kubernetes_manifest_accepts_exactly_one_kubernetes_driver_with_both_capabilities(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    kubernetes_gateway: _Sandboxes,
) -> None:
    settings, project = kubernetes_deployment
    control = NativeWorkerControl(settings, project)
    control.connect()
    assert control.gateway == WorkerGatewayReport(
        decision="accepted",
        version=_GATEWAY_VERSION,
        driver="openshell/kubernetes",
        capabilities=_GATEWAY_CAPABILITIES,
    )
    control.close()
    worker_native.require_gateway_capabilities(settings, project)
    assert kubernetes_gateway.closed


_KUBERNETES = "openshell/kubernetes"


@pytest.mark.parametrize(
    ("drivers", "recorded"),
    [
        pytest.param(None, _UNREADABLE, id="unreadable"),
        pytest.param([], _refused(None), id="no-driver"),
        pytest.param(
            [("openshell/docker", _GATEWAY_CAPABILITIES)],
            _refused("openshell/docker", *_GATEWAY_CAPABILITIES),
            id="docker-driver",
        ),
        pytest.param([(_KUBERNETES, ())], _refused(_KUBERNETES), id="no-capabilities"),
        pytest.param(
            [(_KUBERNETES, (_DELETE_RETRY,))],
            _refused(_KUBERNETES, _DELETE_RETRY),
            id="no-provisioning-wait",
        ),
        pytest.param(
            [(_KUBERNETES, (_DELETE_AWAITS_PROVISIONING,))],
            _refused(_KUBERNETES, _DELETE_AWAITS_PROVISIONING),
            id="no-retry",
        ),
        pytest.param([(_KUBERNETES, _GATEWAY_CAPABILITIES)] * 2, _refused(None), id="ambiguous"),
        pytest.param(
            [(_KUBERNETES, _GATEWAY_CAPABILITIES), ("openshell/docker", _GATEWAY_CAPABILITIES)],
            _refused(None),
            id="both-drivers",
        ),
    ],
)
def test_kubernetes_manifest_refuses_every_other_driver_set_before_any_request(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    kubernetes_gateway: _Sandboxes,
    drivers: list[tuple[str, tuple[str, ...]]] | None,
    recorded: WorkerGatewayReport,
) -> None:
    settings, project = kubernetes_deployment
    if drivers is None:
        kubernetes_gateway.version = None
    else:
        kubernetes_gateway.drivers = drivers
    with pytest.raises(WorkerGatewayCapabilityError, match="required deletion capabilities"):
        worker_native.require_gateway_capabilities(settings, project)
    control = NativeWorkerControl(settings, project)
    with pytest.raises(WorkerGatewayCapabilityError):
        control.connect()
    assert control.gateway == recorded
    assert control.client is None
    assert kubernetes_gateway.closed
    assert kubernetes_gateway.spec is None
    assert kubernetes_gateway.deleted == []
    assert not (settings.native_config_home.parent / "kubectl-used").exists()


def test_missing_capability_diagnostic_names_the_kubernetes_driver_version(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    kubernetes_gateway: _Sandboxes,
) -> None:
    kubernetes_gateway.drivers = [(_KUBERNETES, (_DELETE_RETRY,))]
    with pytest.raises(WorkerGatewayCapabilityError) as raised:
        worker_native.require_gateway_capabilities(*kubernetes_deployment)
    assert str(raised.value).endswith(
        f"version {_GATEWAY_VERSION} lacks {_DELETE_AWAITS_PROVISIONING}"
    )
    kubernetes_gateway.drivers = [("openshell/docker", ())]
    with pytest.raises(WorkerGatewayCapabilityError) as raised:
        worker_native.require_gateway_capabilities(*kubernetes_deployment)
    assert str(raised.value).endswith("required deletion capabilities")


def test_kubernetes_manifest_stops_launch_and_reconciliation_before_any_request(
    kubernetes_deployment: tuple[KubernetesWorkerDeployment, ProjectContext],
    kubernetes_gateway: _Sandboxes,
) -> None:
    settings, project = kubernetes_deployment
    project.state_root.mkdir(parents=True, mode=0o700)
    request = cast(
        WorkerSessionRequest,
        SimpleNamespace(
            project=project,
            session_id="synthetic",
            profile=SimpleNamespace(
                profile_id=settings.profile_id,
                model=settings.model,
                policy_endpoint=settings.model_endpoint,
            ),
            client_tools=(),
        ),
    )
    with pytest.raises(WorkerAttemptError, match="requires the openshell-docker driver"):
        native_worker_plan_factory(settings, project)(request)
    assert project.project_id is not None
    record = WorkerAttemptRecord.model_validate(
        {
            "phase": "fenced",
            "intent": WorkerAttemptIntent(
                project_id=project.project_id,
                session_id="synthetic",
                attempt_id=uuid4(),
                conversation_id=uuid4(),
                image_digest=settings.image_digest,
                gateway_policy_sha256="c" * 64,
                sandbox_policy_sha256=settings.sandbox_policy_sha256,
                input_manifest_sha256="d" * 64,
                control_plane=WorkerControlPlane(
                    control_plane_sha256="1" * 64, container_runtime_sha256="2" * 64
                ),
            ),
            "runtime": None,
        }
    )
    kubernetes_gateway.place("hw-" + record.intent.attempt_id.hex[:16], {})
    with pytest.raises(WorkerAttemptError, match="requires the openshell-docker driver"):
        worker_native.native_worker_reconciler(settings, project)(record)
    assert kubernetes_gateway.running
    assert kubernetes_gateway.spec is None
    assert kubernetes_gateway.deleted == []
    assert not (settings.native_config_home.parent / "kubectl-used").exists()


def test_managed_worker_manifest_has_no_declared_gateway_version(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    manifest = deployment[0].model_dump()
    manifest["gateway_versions"] = [_GATEWAY_VERSION]
    with pytest.raises(ValidationError):
        OpenShellWorkerDeployment.model_validate(manifest)


@pytest.mark.parametrize("change", ["control-plane", "container-runtime", "unrecorded"])
def test_reconciliation_only_observes_the_infrastructure_that_ran_the_attempt(
    native: _Native, change: str
) -> None:
    runtime = native.attempt.launch(native.intent)
    record = native.record("fenced", runtime)
    if change == "control-plane":
        authority = native.settings.native_config_home / "openshell/gateways/synthetic/mtls/ca.crt"
        authority.write_text("another synthetic authority, not key material")
    elif change == "container-runtime":
        native.runtime = "9" * 64
    else:
        record = WorkerAttemptRecord.model_validate(
            {
                "schema_version": "heartwood.worker-attempt.v1",
                "phase": "fenced",
                "intent": native.intent.model_copy(update={"control_plane": None}),
                "runtime": runtime,
            }
        )
    reconcile = worker_native.native_worker_reconciler(native.settings, native.project)
    with pytest.raises(WorkerControlPlaneError):
        reconcile(record)
    # Nothing is observed or removed, so absence elsewhere is never recorded as revocation.
    assert native.sandboxes.running
    assert native.sandboxes.deleted == []
    assert len(native.docker.volumes) == 2


def test_launch_refuses_a_container_runtime_that_changed_after_preparation(
    native: _Native,
) -> None:
    native.runtime = "9" * 64
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    assert native.sandboxes.spec is None
    assert native.docker.volumes == {}


def _lose_creation_response(native: _Native, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Time out the creation request; the returned callable completes it in the control plane."""
    create = native.sandboxes.create
    pending: list[tuple[str, dict[str, str]]] = []

    def lost_create(
        *, workspace: str, name: str, spec: object, labels: dict[str, str]
    ) -> SimpleNamespace:
        assert workspace == native.settings.workspace
        native.sandboxes.spec = spec
        pending.append((name, labels))
        raise TimeoutError("synthetic creation timeout")

    def land() -> None:
        name, labels = pending.pop()
        create(
            workspace=native.settings.workspace,
            name=name,
            spec=native.sandboxes.spec,
            labels=labels,
        )

    monkeypatch.setattr(native.sandboxes, "create", lost_create)
    return land


@pytest.mark.parametrize("revocation", ["owner", "reconciliation"])
def test_absence_after_an_uncertain_creation_keeps_the_attempt_fenced_until_it_appears(
    native: _Native, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    land = _lose_creation_response(native, monkeypatch)
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    revoke = (
        native.attempt.revoke
        if revocation == "owner"
        else worker_native.native_worker_reconciler(native.settings, native.project)
    )
    # The creation may still be queued: absence is never recorded as `not-created`.
    for _ in range(3):
        with pytest.raises(WorkerAttemptError, match="absence after an unresolved creation"):
            revoke(native.record("fenced", None))
    assert native.sandboxes.deleted == []
    # The control plane completes the creation, and the next revocation deletes it.
    land()
    evidence = revoke(native.record("fenced", None))
    assert evidence.outcome == "revoked"
    assert evidence.sandbox_id == "sandbox-1"
    assert native.sandboxes.deleted == ["hw-" + native.intent.attempt_id.hex[:16]]
    assert not native.sandboxes.running


@pytest.mark.parametrize(
    ("status", "rejected"),
    [
        ("INVALID_ARGUMENT", True),
        ("PERMISSION_DENIED", True),
        ("UNAUTHENTICATED", True),
        ("FAILED_PRECONDITION", False),
        ("ALREADY_EXISTS", False),
        ("INTERNAL", False),
        ("UNAVAILABLE", False),
        ("DEADLINE_EXCEEDED", False),
    ],
)
def test_only_a_creation_rejected_before_any_record_resolves_from_absence(
    native: _Native, monkeypatch: pytest.MonkeyPatch, status: str, rejected: bool
) -> None:
    def failed_create(**_request: object) -> SimpleNamespace:
        raise native.sandboxes.error(getattr(native.sandboxes.grpc.StatusCode, status))

    monkeypatch.setattr(native.sandboxes, "create", failed_create)
    with pytest.raises(WorkerAttemptError, match="outside reconciliation"):
        native.attempt.launch(native.intent)
    if rejected:
        assert native.attempt.revoke(native.record("fenced", None)).outcome == "not-created"
    else:
        with pytest.raises(WorkerAttemptError, match="absence after an unresolved creation"):
            native.attempt.revoke(native.record("fenced", None))
    # Without a recorded runtime, an ended process cannot tell which outcome it had.
    reconcile = worker_native.native_worker_reconciler(native.settings, native.project)
    with pytest.raises(WorkerAttemptError, match="absence after an unresolved creation"):
        reconcile(native.record("fenced", None))
    assert native.sandboxes.deleted == []


def test_creation_rejection_statuses_were_checked_against_the_pinned_gateway() -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "images/worker/candidate/gateway.json").read_text())
    pinned = (
        manifest["source"]["revision"],
        tuple(patch["sha256"] for patch in manifest["patches"]),
    )
    assert pinned == worker_native._CREATION_REJECTED_GATEWAY, (
        "the pinned sandbox gateway changed: recheck in its creation path that "
        f"{worker_native._CREATION_REJECTED} are returned only before a sandbox is recorded "
        "or requested, then update _CREATION_REJECTED and _CREATION_REJECTED_GATEWAY"
    )


_GATEWAY_PROCESS = """import sys, time
from pathlib import Path
from types import SimpleNamespace
from heartwood.gateway.worker_control import NativeWorkerForward
cli, home = Path(sys.argv[1]), Path(sys.argv[2])
settings = SimpleNamespace(
    openshell_cli=cli, gateway="synthetic", workspace="synthetic", native_config_home=home
)
control = SimpleNamespace(settings=settings, verify_configuration=lambda: None)
forward = NativeWorkerForward(control, "unique-attempt")
print(forward.start(), flush=True)
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_forwarder_does_not_outlive_the_gateway_process(
    deployment: tuple[OpenShellWorkerDeployment, ProjectContext],
) -> None:
    settings, _project = deployment
    _forwarder(
        settings,
        'echo $$ > "$XDG_CONFIG_HOME/forwarder.pid"\n'
        'echo "Forwarding 127.0.0.1:43210 -> unique-attempt:8000"\nexec sleep 60\n',
    )
    gateway = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _GATEWAY_PROCESS,
            str(settings.openshell_cli),
            str(settings.native_config_home),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert gateway.stdout is not None
        assert gateway.stdout.readline().strip() == b"http://127.0.0.1:43210/"
        forwarder = int((settings.native_config_home / "forwarder.pid").read_text())
        assert _alive(forwarder)
        # The gateway process ends without closing anything it owns.
        gateway.kill()
        gateway.wait(timeout=10)
        deadline = monotonic() + 10
        while _alive(forwarder) and monotonic() < deadline:
            sleep(0.05)
        assert not _alive(forwarder)
    finally:
        gateway.kill()
        gateway.wait(timeout=10)
