# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Outside infrastructure of the native-worker cases on the Kubernetes driver.

The cases run in a pod that stands in for the Heartwood gateway pod of one session: it
holds the session's gateway ServiceAccount token and the sandbox gateway's client
certificate. The platform's control service provisioned the session namespace. A second
credential, mounted separately, lets the cases observe and disturb the namespace from
outside the adapter; the adapter never receives it.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
from google.protobuf.json_format import MessageToDict
from worker_native_fixture import PYTHON, TERMINAL_COMMAND, SyntheticProvider, command

from heartwood.gateway import SessionGateway
from heartwood.gateway.worker_deployment import KubernetesWorkerDeployment
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.gateway.worker_kubernetes import KubernetesWorkerVolumes

WORKSPACE = "hw"
WORKER_PODS = "worker-pods.json"
_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_MOUNTS = Path("/qualification")
_PROVIDER_PORT = 19671
# The sandbox policy hash is a property of the policy, image and gateway, which every case
# of one run shares, so one disposable probe learns it for the run.
_policy_hash: list[str] = []


def _kubeconfig(path: Path, token: Path) -> None:
    """Name the cluster of this pod and a token file, without copying the token."""
    path.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {
                        "name": "cluster",
                        "cluster": {
                            "server": "https://kubernetes.default.svc",
                            "certificate-authority": str(_ACCOUNT / "ca.crt"),
                        },
                    }
                ],
                "users": [{"name": "account", "user": {"tokenFile": str(token)}}],
                "contexts": [
                    {"name": "cluster", "context": {"cluster": "cluster", "user": "account"}}
                ],
                "current-context": "cluster",
            }
        )
    )
    path.chmod(0o600)


@contextmanager
def retain_worker_pods(output: Path) -> Iterator[None]:
    """Record what each admission read about a worker's pods, policies and namespace."""
    inspect_worker = KubernetesWorkerVolumes.inspect_worker
    records: list[dict[str, Any]] = []

    def recording(volumes: KubernetesWorkerVolumes, sandbox_id: str) -> None:
        try:
            inspect_worker(volumes, sandbox_id)
        finally:
            # The adapter keeps the record of an inspection before it decides, so a
            # refused worker is recorded too.
            with suppress(Exception):
                if volumes.record is not None:
                    records.append(
                        {
                            "driver": volumes.settings.driver,
                            "sandbox_id": sandbox_id,
                            "claims": dict(volumes.claims),
                            "admission": volumes.record,
                        }
                    )
                    (output / WORKER_PODS).write_text(
                        json.dumps(records, indent=2, sort_keys=True) + "\n"
                    )

    KubernetesWorkerVolumes.inspect_worker = recording  # type: ignore[method-assign]
    try:
        yield
    finally:
        KubernetesWorkerVolumes.inspect_worker = inspect_worker  # type: ignore[method-assign]


class KubernetesFixture:
    """Own the synthetic provider and outside access; Heartwood owns every actual attempt."""

    hostile = False
    model_profile = None

    def __init__(self, private: Path, output: Path) -> None:
        self.private, self.output = private, output
        self.namespace = os.environ["HW_SESSION_NAMESPACE"]
        self.worker = os.environ["HW_WORKER_IMAGE"]
        self.gateway_image = os.environ["HW_GATEWAY_IMAGE"]
        self.endpoint = os.environ["HW_OPENSHELL_ENDPOINT"]
        self.provider_host = os.environ["HW_PROVIDER_HOST"]
        self.base_url = f"https://{self.provider_host}:{_PROVIDER_PORT}/v1"
        self.identity = "hw-k8s-" + uuid4().hex[:12]
        self.terminal_command = TERMINAL_COMMAND
        self.cli = Path("/opt/qualification/bin/openshell")
        self.kubectl = Path("/opt/qualification/bin/kubectl")
        self.images = json.loads(Path("/opt/qualification/gateway.json").read_text())["images"]
        self.config = private / "config"
        self.env = {"PATH": os.defpath, "XDG_CONFIG_HOME": str(self.config)}
        self.resources = ExitStack()
        self.applications: list[SessionGateway] = []
        self.probes: dict[str, str | None] = {}

    def bind_records(self, project: object) -> Callable[[str], Any]:
        raise NotImplementedError("The Kubernetes cases keep their audit in the project state")

    def cli_command(self, arguments: list[str], **environment: str) -> str:
        return command(
            [str(self.cli), *arguments, "--gateway", self.identity],
            env={**self.env, **environment},
        )

    def _run(self, kubeconfig: Path, arguments: tuple[str, ...], stdin: str | None) -> Any:
        return subprocess.run(
            [
                str(self.kubectl),
                "--kubeconfig",
                str(kubeconfig),
                "--namespace",
                self.namespace,
                "--cache-dir",
                str(self.private / "kubectl-cache"),
                "--request-timeout=30s",
                *arguments,
            ],
            input=stdin,
            env={"PATH": os.defpath, "HOME": str(self.private)},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    def outside(self, *arguments: str, stdin: str | None = None) -> str:
        """Act in the session namespace with the fixture's own credential."""
        result = self._run(self.private / "fixture-kubeconfig", arguments, stdin)
        if result.returncode != 0:
            raise AssertionError("Outside cluster access failed: " + result.stderr.strip())
        return str(result.stdout)

    def as_gateway(self, *arguments: str, stdin: str | None = None) -> tuple[int, str]:
        """Send one request with the credential the adapter uses and return its refusal."""
        result = self._run(self.private / "kubeconfig", arguments, stdin)
        return result.returncode, str(result.stderr).strip()

    def objects(self, *, attempt_id: str | None = None) -> list[str]:
        """List, from outside, the sandboxes, pods and claims of one attempt or of all."""
        listed = json.loads(
            self.outside(
                "get",
                "sandboxes.agents.x-k8s.io,pods,persistentvolumeclaims",
                "--output=json",
            )
        )["items"]
        found = []
        for item in listed:
            metadata = item["metadata"]
            labels = metadata.get("labels") or {}
            # The session's own sandbox gateway and its volume are no worker objects.
            if labels.get("app.kubernetes.io/instance") == "openshell" or metadata[
                "name"
            ].startswith("openshell-data-"):
                continue
            name = f"{item['kind']}/{metadata['name']}"
            if attempt_id is None or attempt_id in json.dumps(item):
                found.append(name)
        return sorted(found)

    def start(self) -> None:
        from openshell import SandboxClient, TlsConfig, WorkspaceClient
        from openshell._proto import sandbox_pb2 as policy_pb

        self.resources.enter_context(retain_worker_pods(self.output))
        _kubeconfig(self.private / "kubeconfig", _ACCOUNT / "token")
        _kubeconfig(self.private / "fixture-kubeconfig", _MOUNTS / "fixture/token")
        mtls = self.config / "openshell/gateways" / self.identity / "mtls"
        mtls.mkdir(parents=True, mode=0o700)
        for name in ("ca.crt", "tls.crt", "tls.key"):
            shutil.copyfile(_MOUNTS / "gateway" / f"openshell-{name}", mtls / name)
            (mtls / name).chmod(0o600)
        for path in self.config.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
        command(
            [str(self.cli), "gateway", "add", self.endpoint, "--name", self.identity, "--local"],
            env=self.env,
        )
        self.control = SandboxClient(
            self.endpoint.removeprefix("https://"),
            timeout=15,
            tls=TlsConfig(
                ca_path=mtls / "ca.crt", cert_path=mtls / "tls.crt", key_path=mtls / "tls.key"
            ),
        )
        self.resources.callback(self.control.close)
        self.control.health()
        workspaces = WorkspaceClient.from_sandbox_client(self.control)
        try:
            workspaces.create(WORKSPACE)
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise
        self.provider = SyntheticProvider(_MOUNTS / "provider")
        self.resources.callback(self.provider.close)
        profile = {
            "id": self.identity,
            "display_name": "Synthetic Qualification",
            "category": "inference",
            "inference_capable": True,
            "credentials": [
                {
                    "name": "api_key",
                    "env_vars": ["HEARTWOOD_QUALIFICATION_KEY"],
                    "required": True,
                    "auth_style": "bearer",
                    "header_name": "authorization",
                }
            ],
            "endpoints": [
                {
                    "host": self.provider_host,
                    "port": _PROVIDER_PORT,
                    "path": "/v1/chat/completions",
                    "protocol": "rest",
                    "enforcement": "enforce",
                    "rules": [{"allow": {"method": "POST", "path": "/v1/chat/completions"}}],
                }
            ],
            "binaries": [os.environ["HW_WORKER_INTERPRETER"]],
        }
        profile_path = self.output / "provider-profile.json"
        profile_path.write_text(json.dumps(profile, indent=2) + "\n")
        self.cli_command(["provider", "profile", "import", "--file", str(profile_path)])
        self.resources.callback(self.cli_command, ["provider", "profile", "delete", self.identity])
        self.cli_command(
            [
                "provider",
                "create",
                "--name",
                self.identity,
                "--type",
                self.identity,
                "--credential",
                "HEARTWOOD_QUALIFICATION_KEY",
            ],
            HEARTWOOD_QUALIFICATION_KEY=self.provider.credential,
        )
        self.resources.callback(self.cli_command, ["provider", "delete", self.identity])
        self.policy = policy_pb.SandboxPolicy(
            version=1,
            filesystem=policy_pb.FilesystemPolicy(
                include_workdir=True,
                read_only=[
                    "/usr",
                    "/bin",
                    "/lib",
                    "/etc",
                    "/proc",
                    "/dev/urandom",
                    "/opt",
                    "/inputs",
                    "/skills",
                ],
                read_write=[
                    "/tmp",
                    "/dev/null",
                    "/home/heartwood",
                    "/dev/pts",
                    "/dev/ptmx",
                    "/workspace",
                ],
            ),
            landlock=policy_pb.LandlockPolicy(compatibility="hard_requirement"),
            process=policy_pb.ProcessPolicy(run_as_user="10001", run_as_group="10001"),
        )

    def exec(self, worker: str, arguments: list[str], *, timeout: int = 30) -> Any:
        """Run a probe inside a worker through the sandbox gateway."""
        return self.control.exec(
            worker, arguments, workspace=WORKSPACE, timeout_seconds=timeout, no_login_shell=True
        )

    def create_probe(self, image: str) -> tuple[str, str]:
        """Create a disposable sandbox with the workers' provider, policy and limits."""
        from openshell._proto import openshell_pb2 as pb

        name = "probe-" + uuid4().hex[:12]
        self.probes[name] = None
        labels = {"heartwood.project": self.identity, "heartwood.attempt": name}
        created = self.control.create(
            workspace=WORKSPACE,
            name=name,
            labels=labels,
            spec=pb.SandboxSpec(
                template=pb.SandboxTemplate(
                    image=image,
                    resources={"limits": {"cpu": "1", "memory": "2Gi"}},
                    labels={**labels, "heartwood.role": "worker"},
                ),
                policy=self.policy,
                providers=[self.identity],
                environment={
                    "SESSION_API_KEY": secrets.token_urlsafe(32),
                    "HEARTWOOD_SKILL_PLATFORM": "generic",
                },
                command=[PYTHON, "-I", "-B", "-u", "/opt/heartwood/images/worker/launch.py"],
            ),
        )
        self.probes[name] = created.id
        return name, created.id

    def revoke_probes(self) -> None:
        """Delete every probe and require the gateway to report each one deleted."""
        for name, sandbox_id in tuple(self.probes.items()):
            for _ in range(5):
                try:
                    self.control.delete(
                        name, workspace=WORKSPACE, allow_missing=True, timeout_seconds=90
                    )
                    break
                except grpc.RpcError:
                    continue
            else:
                raise RuntimeError("Probe revocation is uncertain; retaining control state")
            if sandbox_id is not None:
                self.control.wait_deleted(
                    name, workspace=WORKSPACE, expected_sandbox_id=sandbox_id, timeout_seconds=90
                )
            del self.probes[name]

    def qualify_policy(self) -> str:
        """Admit a disposable probe before selecting the launch policy hash."""
        from openshell._proto import openshell_pb2 as pb

        if _policy_hash:
            return _policy_hash[0]
        name, sandbox_id = self.create_probe(self.worker)
        try:
            ready = self.control.wait_ready(name, workspace=WORKSPACE, timeout_seconds=240)
            admission = ready.status.configuration_admission
            if (
                ready.id != sandbox_id
                or not ready.status.configuration_activated
                or admission is None
                or admission.state != pb.CONFIGURATION_ADMISSION_STATE_ACCEPTED
            ):
                raise RuntimeError("Independent native policy probe was not admitted")
            _policy_hash.append(str(admission.policy_hash))
            return _policy_hash[0]
        finally:
            self.revoke_probes()

    def deployment(self, inputs: WorkerInputManifest, **changes: Any) -> KubernetesWorkerDeployment:
        policy_path = self.private / "policy.json"
        policy_path.write_text(json.dumps(MessageToDict(self.policy)))
        policy_path.chmod(0o600)
        repository, _, digest = self.worker.partition("@")
        settings: dict[str, Any] = {
            "driver": "openshell-kubernetes",
            "gateway": self.identity,
            "native_config_home": self.config,
            "openshell_cli": self.cli,
            "kubectl_cli": self.kubectl,
            "kubeconfig": self.private / "kubeconfig",
            "namespace": self.namespace,
            "image_repository": repository,
            "supervisor_image_digest": self.images["supervisor"].partition("@")[2],
            "sandbox_image_digest": self.images["sandbox"].partition("@")[2],
            "workspace": WORKSPACE,
            "image_digest": digest,
            "sandbox_policy_sha256": self.qualify_policy(),
            "policy_path": policy_path,
            "policy_file_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            "profile_id": "synthetic",
            "model": "openai/synthetic-boundary",
            "model_endpoint": self.base_url + "/chat/completions",
            "provider": self.identity,
            "placeholder_env": "HEARTWOOD_QUALIFICATION_KEY",
            "inputs": inputs,
            "cpu": "1",
            "memory": "2Gi",
            **changes,
        }
        return KubernetesWorkerDeployment.model_validate(settings)
