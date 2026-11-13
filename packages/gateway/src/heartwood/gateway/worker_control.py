# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Use the native sandbox SDK and CLI with one explicit operator-owned TLS identity."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
from collections import deque
from threading import Event, Thread
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from heartwood.core_adapter.worker_attempts import WorkerAttemptError
from heartwood.gateway._deployment import validate_worker_deployment
from heartwood.gateway._project import ProjectContext
from heartwood.gateway.worker_deployment import OpenShellWorkerDeployment
from heartwood.persistence import read_private_text


class NativeWorkerControl:
    """Retain native connection material without selecting ambient gateway credentials."""

    def __init__(self, settings: OpenShellWorkerDeployment, project: ProjectContext) -> None:
        """Validate native connection material without opening a control channel."""
        self.settings = settings
        self.project = project
        validate_worker_deployment(settings, project)
        directory = settings.native_config_home / "openshell" / "gateways" / settings.gateway
        self._files = tuple(
            directory / name
            for name in (
                "metadata.json",
                "mtls/ca.crt",
                "mtls/tls.crt",
                "mtls/tls.key",
            )
        )
        self._fingerprints = self._snapshot()
        metadata = json.loads(read_private_text(directory / "metadata.json", max_bytes=64 * 1024))
        endpoint = urlsplit(metadata.get("gateway_endpoint", ""))
        if (
            metadata.get("name") != settings.gateway
            or metadata.get("auth_mode") != "mtls"
            or endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in ("", "/")
        ):
            raise WorkerAttemptError(
                "native worker gateway requires its registered mutual TLS identity"
            )
        self._endpoint = endpoint.netloc
        try:
            # Optional native runtime dependencies are loaded only at this platform boundary.
            self.sdk = importlib.import_module("openshell")
            self.pb = importlib.import_module("openshell._proto.openshell_pb2")
            self.policy_pb = importlib.import_module("openshell._proto.sandbox_pb2")
            self._json_format = importlib.import_module("google.protobuf.json_format")
            self.grpc = importlib.import_module("grpc")
            if "configuration_admission" not in self.sdk.SandboxStatusRef.__dataclass_fields__:
                raise ImportError
        except (ImportError, AttributeError):
            raise WorkerAttemptError(
                "installed sandbox SDK lacks required admission observations"
            ) from None
        self.client: Any = None

    def _snapshot(self) -> tuple[str, ...]:
        return tuple(
            hashlib.sha256(read_private_text(path, max_bytes=64 * 1024).encode()).hexdigest()
            for path in self._files
        )

    def verify_configuration(self) -> None:
        """Reject changed operator authority before using another native control path."""
        validate_worker_deployment(self.settings, self.project)
        if self._snapshot() != self._fingerprints:
            raise WorkerAttemptError("native gateway identity changed during the worker attempt")

    def connect(self) -> None:
        """Create the maintained SDK client only after the session retains launch intent."""
        self.verify_configuration()
        if self.client is not None:
            raise WorkerAttemptError("native control client is already connected")
        directory = self._files[0].parent / "mtls"
        self.client = self.sdk.SandboxClient(
            self._endpoint,
            tls=self.sdk.TlsConfig(
                ca_path=directory / "ca.crt",
                cert_path=directory / "tls.crt",
                key_path=directory / "tls.key",
            ),
            timeout=15,
        )

    def policy(self) -> object:
        """Parse the operator's native policy without introducing a second policy compiler."""
        content = read_private_text(self.settings.policy_path, max_bytes=64 * 1024)
        policy = self._json_format.ParseDict(json.loads(content), self.policy_pb.SandboxPolicy())
        if (
            policy.landlock.compatibility != "hard_requirement"
            or policy.process.run_as_user != "10001"
            or policy.process.run_as_group != "10001"
            or not {"/inputs", "/skills"}.issubset(policy.filesystem.read_only)
        ):
            raise WorkerAttemptError(
                "native policy does not match the qualified worker identity and projections"
            )
        return policy

    def close(self) -> None:
        """Close the SDK channel after outside revocation, without claiming revocation itself."""
        if self.client is not None:
            self.client.close()
            self.client = None


class NativeWorkerForward:
    """Own the native CLI forwarding process, retaining only a bounded output tail."""

    def __init__(self, control: NativeWorkerControl, name: str) -> None:
        """Retain the exact registered gateway and unique attempt name."""
        self._control = control
        self._name = name
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: Thread | None = None
        self._changed = Event()
        self._tail: deque[bytes] = deque(maxlen=16)

    @property
    def active(self) -> bool:
        """Observe process liveness; sandbox identity is checked separately through the SDK."""
        return self._process is not None and self._process.poll() is None

    def start(self) -> str:
        """Let the native forwarder allocate a loopback port and own the tunnel."""
        if self._process is not None:
            raise WorkerAttemptError("worker forwarding cannot be restarted")
        self._control.verify_configuration()
        settings = self._control.settings
        self._process = subprocess.Popen(
            [
                str(settings.openshell_cli),
                "forward",
                "service",
                self._name,
                "--gateway",
                settings.gateway,
                "--workspace",
                settings.workspace,
                "--target-port",
                "8000",
                "--local",
                "127.0.0.1:0",
                "--color",
                "never",
            ],
            env={"PATH": os.defpath, "XDG_CONFIG_HOME": str(settings.native_config_home)},
            cwd=settings.native_config_home,
            bufsize=0,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._reader = Thread(target=self._drain, daemon=True, name="worker-forward-output")
        self._reader.start()
        deadline = monotonic() + 15
        while self.active and monotonic() < deadline:
            output = b"".join(tuple(self._tail))
            match = re.search(rb"Forwarding (127\.0\.0\.1:[0-9]+) ->", output)
            if match:
                return "http://" + match.group(1).decode() + "/"
            self._changed.wait(timeout=0.1)
            self._changed.clear()
        raise WorkerAttemptError("native worker forwarding requires reconciliation")

    def _drain(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        with self._process.stdout as stream:
            while chunk := stream.read(4096):
                self._tail.append(chunk)
                self._changed.set()
        self._changed.set()

    def close(self) -> None:
        """Stop only this adapter's forwarding process, retaining failures for reconciliation."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
            if self._reader.is_alive():
                raise WorkerAttemptError("native forwarding output did not stop")
