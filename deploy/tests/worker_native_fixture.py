# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Disposable outside infrastructure for the installed native-worker tests."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import shutil
import ssl
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
from google.protobuf.json_format import MessageToDict
from openshell import SandboxClient, TlsConfig
from openshell._proto import openshell_pb2 as pb
from openshell._proto import sandbox_pb2 as policy_pb
from worker_model_profile import QualificationModelProfile
from worker_network_qualification import qualify_network
from worker_telemetry import InfrastructureCollector

from deploy.tests.checkpoint_transport_integration import RecordsTransport, records_transport
from heartwood.core_adapter.checkpoint_evidence import SessionCheckpointEvidence
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway.checkpoint_deployment import deployment_checkpoint_evidence
from heartwood.gateway.worker_deployment import OpenShellWorkerDeployment
from heartwood.gateway.worker_inputs import WorkerInputManifest

PYTHON = "/opt/heartwood/.venv/bin/python"
TERMINAL_COMMAND = (
    f"{PYTHON} /skills/aggregate-export/scripts/run.py --summary /inputs/summary.json "
    "--output /workspace/export.json --aggregate-count-floor 20 && cat /workspace/export.json"
)


def command(arguments: list[str], *, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(
        arguments, env=env, text=True, stderr=subprocess.PIPE, timeout=90
    )


class SyntheticProvider:
    """A real TLS endpoint returning a bounded sequence of native tool calls."""

    def __init__(self, tls: Path) -> None:
        self.credential = secrets.token_urlsafe(32)
        self.observations: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.canary_requests = 0
        self.redirect_requests = 0
        self.forbidden_values: tuple[str, ...] = (self.credential,)
        self.script = [
            ("invoke_skill", {"name": "aggregate-export"}),
            ("terminal", {"command": TERMINAL_COMMAND}),
            ("finish", {"message": "Synthetic aggregate check completed."}),
        ]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                owner.canary_requests += 1
                data = b"synthetic-outside-canary"
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 * 1024:
                        raise ValueError("unexpected inference request length")
                    body = json.loads(self.rfile.read(length))
                    if any(value in json.dumps(body) for value in owner.forbidden_values):
                        raise AssertionError("Outside secret reached the inference request")
                    index = len(owner.observations)
                    authorized = secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + owner.credential
                    )
                    if self.headers.get("X-Heartwood-Qualification-Probe") == "redirect":
                        if self.path != "/v1/chat/completions":
                            raise AssertionError("Redirect probe bypassed the approved endpoint")
                        owner.redirect_requests += 1
                        self.send_response(303)
                        self.send_header("Location", "/v1/forbidden")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    owner.observations.append(
                        {"path": self.path, "authorized": authorized, "model": body.get("model")}
                    )
                    if (
                        not authorized
                        or self.path != "/v1/chat/completions"
                        or index >= len(owner.script)
                    ):
                        self.send_error(403)
                        return
                    name, arguments = owner.script[index]
                    if name not in [tool["function"]["name"] for tool in body.get("tools", [])]:
                        raise ValueError("required tool absent from native request")
                    call = {
                        "id": f"synthetic-{index}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                    common = {"id": f"synthetic-{index}", "created": 1, "model": "synthetic"}
                    usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
                    if body.get("stream"):
                        first = {
                            **common,
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": "",
                                        "tool_calls": [{"index": 0, **call}],
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                        last = {
                            **common,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                            "usage": usage,
                        }
                        data = (
                            f"data: {json.dumps(first)}\n\ndata: {json.dumps(last)}\n\n"
                            "data: [DONE]\n\n"
                        )
                        media = "text/event-stream"
                    else:
                        data = json.dumps(
                            {
                                **common,
                                "object": "chat.completion",
                                "choices": [
                                    {
                                        "index": 0,
                                        "message": {
                                            "role": "assistant",
                                            "content": "",
                                            "tool_calls": [call],
                                        },
                                        "finish_reason": "tool_calls",
                                    }
                                ],
                                "usage": usage,
                            }
                        )
                        media = "application/json"
                    encoded = data.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", media)
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except Exception as error:
                    owner.errors.append(type(error).__name__)
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("0.0.0.0", 19671), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls / "server/tls.crt", tls / "server/tls.key")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive() or self.errors:
            raise RuntimeError(f"Synthetic provider did not finish cleanly: {self.errors}")


class NativeFixture:
    """Own one gateway and synthetic provider; Heartwood owns every actual attempt."""

    def __init__(
        self,
        private: Path,
        output: Path,
        *,
        hostile: bool = False,
        model_profile: QualificationModelProfile | None = None,
        credential: str | None = None,
        provider_deadline_seconds: int | None = None,
    ) -> None:
        self.private, self.output = private, output
        if (model_profile is None) != (credential is None):
            raise ValueError("External qualification requires both a profile and a credential")
        if provider_deadline_seconds is not None and (
            type(provider_deadline_seconds) is not int or not 1 <= provider_deadline_seconds <= 600
        ):
            raise ValueError("Provider lifetime must be between one and 600 seconds")
        self.provider_deadline_seconds = (
            model_profile.deadline_seconds if model_profile else provider_deadline_seconds
        )
        self.inference_deadline: float | None = None
        self.credential_expires_at_ms: int | None = None
        self.model_profile = model_profile
        self._inference_credential = credential
        self.base_url = (
            model_profile.base_url if model_profile else "https://host.docker.internal:19671/v1"
        )
        self.hostile = hostile
        self.terminal_command = TERMINAL_COMMAND
        self.sentinel = secrets.token_urlsafe(32)
        self.protected_canary = private / "outside-canary"
        self.identity = "hw-native-" + uuid4().hex[:12]
        self.cli = Path("/opt/qualification/bin/openshell")
        self.binary = self.cli.with_name("openshell-gateway")
        self.worker = os.environ["HW_WORKER_IMAGE"]
        self.network = os.environ["HW_QUALIFICATION_NETWORK"]
        self.controller_image = os.environ["HW_CONTROLLER_IMAGE"]
        self.gateway_image = os.environ["HW_GATEWAY_IMAGE"]
        self.images = json.loads(Path("/opt/qualification/gateway.json").read_text())["images"]
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OPENSHELL_", "XDG_"))
        }
        self.env.update(
            {
                f"XDG_{kind}_HOME": str(private / kind.lower())
                for kind in ("CONFIG", "STATE", "DATA", "CACHE")
            }
        )
        self.env["OPENSHELL_GATEWAY"] = self.identity
        self.resources = ExitStack()
        self.applications: list[SessionGateway] = []
        self.probes: dict[str, str | None] = {}
        self.records: RecordsTransport | None = None
        self.evidence: Callable[[str], SessionCheckpointEvidence] | None = None

    def bind_records(self, project: ProjectContext) -> Callable[[str], SessionCheckpointEvidence]:
        """Attach the same outside records deployment used by transport qualification."""
        assert self.records is None
        self.records = self.resources.enter_context(
            records_transport(
                Path("/opt/qualification/bin/minio"),
                Path("/opt/qualification/bin/mc"),
                self.controller_image,
                self.private / "records",
                project=project,
                session_id="composition",
            )
        )
        self.provider.forbidden_values += self.records.forbidden_values
        self.collector.forbidden_values += self.records.forbidden_values
        self.evidence = self.resources.enter_context(
            deployment_checkpoint_evidence(self.records.settings, project)
        )
        return self.evidence

    def cli_command(self, arguments: list[str], **environment: str) -> str:
        return command([str(self.cli), *arguments], env={**self.env, **environment})

    def qualify_telemetry(self, worker_name: str, sandbox_id: str) -> dict[str, object]:
        """Observe native attribution and an explicit exporter gap at the outside collector."""
        collector = self.collector

        def supervisor_records() -> list[dict[str, Any]]:
            return [
                item
                for item in collector.observations()
                if item["resource"].get("openshell.sandbox.id") == sandbox_id
            ]

        collector.wait_for(lambda: bool(supervisor_records()))
        for item in supervisor_records():
            assert item["resource"].get("openshell.gateway.name") == self.identity
        try:
            collector.unavailable(True)
            deadline = time.monotonic() + 90
            # Native loss notifications accompany the next event batch. Keep a
            # bounded source active until the exporter reports an actual loss.
            while not collector.observed_loss(sandbox_id):
                assert time.monotonic() < deadline, "Native exporter did not report lost records"
                result = self.control.exec(
                    worker_name,
                    ["/bin/true"],
                    workspace="default",
                    timeout_seconds=15,
                    no_login_shell=True,
                )
                assert result.exit_code == 0
                time.sleep(0.5)
        finally:
            collector.unavailable(False)
        result = self.control.exec(
            worker_name,
            ["/bin/true"],
            workspace="default",
            timeout_seconds=15,
            no_login_shell=True,
        )
        assert result.exit_code == 0

        def gaps() -> list[dict[str, Any]]:
            return [
                item
                for item in collector.observations()
                if item["record"].get("event_name") == "openshell.ocsf.gap"
                and item["resource"].get("openshell.sandbox.id") == sandbox_id
            ]

        collector.wait_for(lambda: bool(gaps()))
        for gap in gaps():
            attributes = {item["key"]: item["value"] for item in gap["record"]["attributes"]}
            assert int(attributes["openshell.ocsf.gap.dropped"]["int_value"]) > 0
            assert int(attributes["openshell.ocsf.gap.since_unix_ms"]["int_value"]) > 0
        native = supervisor_records()
        identities = set()
        for item in native:
            if item["record"].get("event_name") == "openshell.ocsf.gap":
                continue
            record = json.loads(item["record"]["body"]["string_value"])
            assert record["metadata"]["uid"]
            identities.add(record["metadata"]["uid"])
        assert identities
        content = collector.path.read_text()
        assert self.provider.credential not in content
        assert self.sentinel not in content
        return {
            "transport": "native-otlp-mtls",
            "unauthenticated_collector_client_rejected": True,
            "sandbox_id": sandbox_id,
            "gateway_id": self.identity,
            "supervisor_records": len(native),
            "unique_native_event_ids": len(identities),
            "exporter_gap_records": len(gaps()),
            "collection_complete": False,
            "loss": "synthetic collector outage exhausted native retries",
            "artifact": collector.path.name,
        }

    def start(self) -> None:
        controller = json.loads(command(["docker", "inspect", os.environ["HW_CONTROLLER_NAME"]]))[0]
        if self.network not in controller["NetworkSettings"]["Networks"]:
            command(
                ["docker", "network", "connect", self.network, os.environ["HW_CONTROLLER_NAME"]]
            )
            controller = json.loads(
                command(["docker", "inspect", os.environ["HW_CONTROLLER_NAME"]])
            )[0]
        network = controller["NetworkSettings"]["Networks"][self.network]
        control_address = ipaddress.IPv4Address(
            controller["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        )
        self.ipv4 = ipaddress.IPv4Address(network["IPAddress"])
        self.ipv6 = ipaddress.IPv6Address(network["GlobalIPv6Address"])

        tls = self.private / "tls"
        command(
            [
                str(self.binary),
                "generate-certs",
                "--output-dir",
                str(tls),
                "--server-san",
                "localhost",
                "--server-san",
                "127.0.0.1",
                "--server-san",
                "host.docker.internal",
                "--server-san",
                str(control_address),
            ]
        )
        for path in tls.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        self.collector = InfrastructureCollector(tls, self.output)
        self.resources.callback(self.collector.close)
        self.collector.verify_client_auth(tls)

        def q(value: object) -> str:
            return json.dumps(str(value))

        config = self.private / "gateway.toml"
        config.write_text(f"""[openshell]
version = 2
[openshell.gateway]
name = {q(self.identity)}
bind_address = "0.0.0.0:19670"
compute_driver = "docker"
enable_loopback_service_http = false
guest_tls_ca = {q(tls / "ca.crt")}
[openshell.gateway.tls]
cert_path = {q(tls / "server/tls.crt")}
key_path = {q(tls / "server/tls.key")}
client_ca_path = {q(tls / "ca.crt")}
[openshell.gateway.gateway_jwt]
signing_key_path = {q(tls / "jwt/signing.pem")}
public_key_path = {q(tls / "jwt/public.pem")}
kid_path = {q(tls / "jwt/kid")}
gateway_id = {q(self.identity)}
[openshell.gateway.otlp.ocsf]
enabled = true
endpoint = {q(f"https://{control_address}:{self.collector.port}")}
ca_file = {q(tls / "ca.crt")}
cert_file = {q(tls / "client/tls.crt")}
key_file = {q(tls / "client/tls.key")}
[openshell.drivers.docker]
socket_path = "/var/run/docker.sock"
supervisor_network_mode = "bridge"
host_gateway_ip = {q(control_address)}
grpc_endpoint = "https://host.docker.internal:19670"
sandbox_label = {q(self.identity)}
sandbox_runtime_image = {q(self.images["sandbox"])}
supervisor_image = {q(self.images["supervisor"])}
image_pull_policy = "if_not_present"
allow_driver_config = true
enable_bind_mounts = false
[openshell.drivers.docker.resource_admission]
enabled = true
[openshell.drivers.docker.resource_admission.required_labels]
"openshell.ai/sandbox-attachable" = "true"
"openshell.ai/sandbox-attachable-workspace" = "${{workspace}}"
[openshell.supervisor.network]
additional_ca_bundle = {q(tls / "ca.crt")}
""")
        config.chmod(0o600)
        command([str(self.binary), "--config", str(config), "config", "preflight"], env=self.env)
        arguments = [
            "docker",
            "run",
            "--detach",
            "--name",
            self.identity,
            "--label",
            "heartwood-qualification-owner=" + self.identity,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--group-add",
            str(Path("/run/docker.sock").stat().st_gid),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--memory",
            "1g",
            "--network",
            "container:" + os.environ["HW_CONTROLLER_NAME"],
            "--mount",
            f"type=bind,src={self.private},dst={self.private}",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
        ]
        for name, value in self.env.items():
            if name.startswith("XDG_"):
                arguments.extend(["--env", f"{name}={value}"])
        command(
            [
                *arguments,
                "--env",
                "HOME=" + str(self.private),
                self.gateway_image,
                "--config",
                str(config),
            ]
        )
        self.resources.callback(self._stop_gateway)
        self.control = SandboxClient(
            "127.0.0.1:19670",
            timeout=15,
            tls=TlsConfig(
                ca_path=tls / "ca.crt",
                cert_path=tls / "client/tls.crt",
                key_path=tls / "client/tls.key",
            ),
        )
        self.resources.callback(self.control.close)
        for _ in range(60):
            try:
                self.control.health()
                break
            except grpc.RpcError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Disposable native gateway did not become healthy")
        mtls = self.private / "config/openshell/gateways" / self.identity / "mtls"
        mtls.mkdir(parents=True, mode=0o700)
        for source, name in (
            ("ca.crt", "ca.crt"),
            ("client/tls.crt", "tls.crt"),
            ("client/tls.key", "tls.key"),
        ):
            shutil.copyfile(tls / source, mtls / name)
            (mtls / name).chmod(0o600)
        self.cli_command(
            ["gateway", "add", "https://127.0.0.1:19670", "--name", self.identity, "--local"]
        )
        self.provider = SyntheticProvider(tls)
        self.collector.forbidden_values = (self.provider.credential, self.sentinel)
        self.resources.callback(self.provider.close)
        if self._inference_credential is not None:
            self.provider.forbidden_values += (self._inference_credential,)
            self.collector.forbidden_values += (self._inference_credential,)
        self.protected_canary.write_text(self.sentinel)
        self.protected_canary.chmod(0o600)
        self.provider.forbidden_values += (self.sentinel,)
        interpreter = command(
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
                PYTHON,
                self.worker,
                "-I",
                "-c",
                "from pathlib import Path;import sys;print(Path(sys.executable).resolve())",
            ]
        ).strip()
        profile: dict[str, Any] = {
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
                    "host": "host.docker.internal",
                    "port": 19671,
                    "path": "/v1/chat/completions",
                    "protocol": "rest",
                    "enforcement": "enforce",
                    "rules": [{"allow": {"method": "POST", "path": "/v1/chat/completions"}}],
                }
            ],
            "binaries": [interpreter],
        }
        if self.model_profile is not None:
            from urllib.parse import urlsplit

            profile["endpoints"][0].update(
                host=urlsplit(self.model_profile.base_url).hostname,
                port=443,
            )
        profile_path = self.output / "provider-profile.json"
        profile_path.write_text(json.dumps(profile, indent=2) + "\n")
        self.cli_command(["provider", "profile", "import", "--file", str(profile_path)])
        self.resources.callback(self.cli_command, ["provider", "profile", "delete", self.identity])
        self._create_inference_provider()
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
                read_write=["/tmp", "/dev/null", "/home/heartwood", "/dev/pts", "/dev/ptmx"],
            ),
            landlock=policy_pb.LandlockPolicy(compatibility="hard_requirement"),
            process=policy_pb.ProcessPolicy(run_as_user="10001", run_as_group="10001"),
        )

    def _create_inference_provider(self) -> None:
        """Install authorization expiry before any worker can attach the provider."""
        provider_arguments = [
            "provider",
            "create",
            "--name",
            self.identity,
            "--type",
            self.identity,
            "--credential",
            "HEARTWOOD_QUALIFICATION_KEY",
        ]
        if self.provider_deadline_seconds is not None:
            self.inference_deadline = time.monotonic() + self.provider_deadline_seconds
            self.credential_expires_at_ms = (
                int(time.time() * 1000) + self.provider_deadline_seconds * 1000
            )
            provider_arguments.extend(
                [
                    "--credential-expires-at",
                    f"HEARTWOOD_QUALIFICATION_KEY={self.credential_expires_at_ms}",
                ]
            )
        self.cli_command(
            provider_arguments,
            HEARTWOOD_QUALIFICATION_KEY=self._inference_credential or self.provider.credential,
        )
        self.resources.callback(self.cli_command, ["provider", "delete", self.identity])

    def prepare_hostile(self, protected_state: Path) -> None:
        """Keep the writable-state canary in the actual bound application state."""
        self.protected_canary = protected_state / "synthetic-canary"
        self.protected_canary.write_text(self.sentinel)
        self.protected_canary.chmod(0o600)
        probe = Path(__file__).with_name("worker_hostile_probe.py").read_text()
        arguments = [
            PYTHON,
            "-I",
            "-B",
            "-c",
            probe,
            "--protected",
            str(self.protected_canary),
            "--credential",
            str(self.private / "tls/client/tls.key"),
            "--endpoint",
            "https://host.docker.internal:19671/v1/forbidden",
        ]
        self.terminal_command = shlex.join(arguments) + " && " + TERMINAL_COMMAND
        self.provider.script[1] = ("terminal", {"command": self.terminal_command})

    def check_outside_canary(self) -> None:
        """Prove the forbidden destination works from outside the worker boundary."""
        context = ssl.create_default_context(cafile=self.private / "tls/ca.crt")
        with urllib.request.urlopen(
            "https://127.0.0.1:19671/v1/forbidden", context=context, timeout=5
        ) as response:
            if response.status != 200 or response.read() != b"synthetic-outside-canary":
                raise AssertionError("Outside provider canary is unavailable")
        if self.protected_canary.read_text() != self.sentinel:
            raise AssertionError("Outside state canary changed")

    def qualify_direct_network(self, worker: str) -> dict[str, object]:
        """Verify actual dual-stack routes from a separate, unprivileged outside client."""
        program = (
            "import ipaddress,sys;sys.path.insert(0,'/opt/qualification/tests');"
            "from worker_network_qualification import send_positive_controls;"
            "send_positive_controls(ipaddress.IPv4Address(sys.argv[1]),"
            "ipaddress.IPv6Address(sys.argv[2]),19672,19673)"
        )

        def peer() -> None:
            command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    self.network,
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--memory",
                    "128m",
                    "--pids-limit",
                    "32",
                    "--user",
                    "10001:10001",
                    "--entrypoint",
                    PYTHON,
                    self.controller_image,
                    "-I",
                    "-B",
                    "-c",
                    program,
                    str(self.ipv4),
                    str(self.ipv6),
                ]
            )

        return qualify_network(
            self.control,
            worker,
            workspace="default",
            ipv4=self.ipv4,
            ipv6=self.ipv6,
            port=19672,
            ipv6_port=19673,
            peer=peer,
        )

    def _stop_gateway(self) -> None:
        observed = json.loads(command(["docker", "inspect", self.identity]))[0]
        if observed["Config"]["Labels"].get("heartwood-qualification-owner") != self.identity:
            raise RuntimeError("Refusing cleanup of a gateway with changed ownership")
        logs = subprocess.run(
            ["docker", "logs", self.identity], capture_output=True, text=True, timeout=15
        )
        rendered = logs.stdout + logs.stderr
        if self._inference_credential is not None and self._inference_credential in rendered:
            rendered = rendered.replace(self._inference_credential, "[redacted]")
            (self.output / "gateway.log").write_text(rendered)
            raise RuntimeError("Native gateway logs contained protected qualification content")
        (self.output / "gateway.log").write_text(rendered)
        command(["docker", "stop", "--time", "20", observed["Id"]])
        command(["docker", "rm", observed["Id"]])

    def create_probe(self, image: str) -> tuple[str, str]:
        """Retain a probe's identity before observing its startup or failure."""
        name = "probe-" + uuid4().hex[:12]
        self.probes[name] = None
        created = self.control.create(
            workspace="default",
            name=name,
            labels={"qualification": self.identity},
            spec=pb.SandboxSpec(
                template=pb.SandboxTemplate(
                    image=image,
                    resources={"limits": {"cpu": "1", "memory": "2Gi"}},
                ),
                policy=self.policy,
                providers=[self.identity],
                environment={"SESSION_API_KEY": secrets.token_urlsafe(32)},
                command=[PYTHON, "-I", "-B", "-u", "/opt/heartwood/images/worker/launch.py"],
            ),
        )
        self.probes[name] = created.id
        return name, created.id

    def qualify_policy(self) -> str:
        """Admit a disposable probe before selecting the production launch policy hash."""
        name, sandbox_id = self.create_probe(self.worker)
        try:
            ready = self.control.wait_ready(name, workspace="default", timeout_seconds=90)
            admission = ready.status.configuration_admission
            if (
                ready.id != sandbox_id
                or not ready.status.configuration_activated
                or admission is None
                or admission.state != pb.CONFIGURATION_ADMISSION_STATE_ACCEPTED
            ):
                raise RuntimeError("Independent native policy probe was not admitted")
            return str(admission.policy_hash)
        finally:
            self.revoke_probes()

    def revoke_probes(self) -> None:
        """Observe probe deletion before releasing its provider or control plane."""
        for name in tuple(self.probes):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    observed = self.control.get(name, workspace="default")
                except grpc.RpcError as error:
                    if error.code() == grpc.StatusCode.NOT_FOUND and self.probes[name] is not None:
                        self.control.wait_deleted(
                            name,
                            workspace="default",
                            expected_sandbox_id=self.probes[name],
                            timeout_seconds=15,
                        )
                        del self.probes[name]
                        break
                    time.sleep(0.5)
                    continue
                expected = self.probes[name]
                if (expected is not None and observed.id != expected) or dict(observed.labels) != {
                    "qualification": self.identity
                }:
                    raise RuntimeError("Policy probe ownership changed; retaining control state")
                self.probes[name] = observed.id
                try:
                    self.control.delete(name, workspace="default", timeout_seconds=15)
                    self.control.wait_deleted(
                        name,
                        workspace="default",
                        expected_sandbox_id=observed.id,
                        timeout_seconds=15,
                    )
                except grpc.RpcError:
                    # Ready can precede settled provisioning; reconcile the same identity.
                    time.sleep(0.5)
                    continue
                del self.probes[name]
                break
            else:
                raise RuntimeError("Policy probe revocation is uncertain; retaining control state")

    def deployment(self, inputs: WorkerInputManifest) -> OpenShellWorkerDeployment:
        policy_path = self.private / "policy.json"
        policy_path.write_text(json.dumps(MessageToDict(self.policy)))
        policy_path.chmod(0o600)
        docker = self.private / "docker"
        source = shutil.which("docker")
        if source is None:
            raise RuntimeError("Docker client absent from qualification controller")
        shutil.copyfile(source, docker)
        docker.chmod(0o500)
        return OpenShellWorkerDeployment(
            driver="openshell-docker",
            gateway=self.identity,
            native_config_home=self.private / "config",
            openshell_cli=self.cli,
            docker_cli=docker,
            docker_host="unix:///run/docker.sock",
            docker_socket_group=Path("/run/docker.sock").stat().st_gid,
            workspace="default",
            image_digest=self.worker,
            sandbox_policy_sha256=self.qualify_policy(),
            policy_path=policy_path,
            policy_file_sha256=hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            profile_id="synthetic",
            model=self.model_profile.model if self.model_profile else "openai/synthetic-boundary",
            model_endpoint=self.base_url + "/chat/completions",
            provider=self.identity,
            placeholder_env="HEARTWOOD_QUALIFICATION_KEY",
            inputs=inputs,
            cpu="1",
            memory="2Gi",
        )
