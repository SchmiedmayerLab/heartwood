# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The files of a hosted gateway that the control service writes for a session."""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from heartwood_platform.control.hosted import (
    HostedGateway,
    HostedGatewaySettings,
    sandbox_digests,
)

from heartwood.schemas import PlatformLaunchContext

_OBJECTS = (
    Path(__file__).resolve().parents[2]
    / "chart"
    / "heartwood-platform"
    / "files"
    / "openshell-session.json"
)
HOSTED: dict[str, Any] = {
    "control_url": "https://heartwood-control.heartwood-platform.svc.cluster.local:8443",
    "worker": {
        "image": "registry.example.test:5000/heartwood-worker:candidate@sha256:" + "2" * 64,
        "sandbox_policy_sha256": "3" * 64,
        "policy": {"version": 1, "process": {"runAsUser": "10001"}},
        "cpu": "1",
        "memory": "2Gi",
    },
    "model_route": {
        "profile_id": "platform-route",
        "model": "platform/no-route",
        "endpoint": "https://model-route.invalid/v1/chat/completions",
        "provider": "platform-route",
        "placeholder_env": "HEARTWOOD_PLATFORM_MODEL_KEY",
    },
}
_LAUNCH = PlatformLaunchContext(
    project_context_id="abc123",
    platform_id="reference-platform",
    project_id="demo",
    billing_attribution_ref="grant",
    authority_ref="reference-platform",
    policy_ref="policy",
    issued_at=datetime(2026, 11, 19, tzinfo=UTC),
)


def _files(session: str = "abc123") -> dict[str, str]:
    supervisor, sandbox = sandbox_digests(_OBJECTS.read_text(encoding="utf-8"))
    gateway = HostedGateway(
        HostedGatewaySettings.model_validate(HOSTED),
        issuer="https://keycloak.example.test/realms/heartwood",
        supervisor=supervisor,
        sandbox=sandbox,
    )
    return gateway.files(
        session=session,
        subject="6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001",
        client_id="hw-proj-demo",
        namespace=f"hw-s-{session}",
        launch_context=_LAUNCH,
    )


def test_manifest_binds_the_researcher_the_session_worker_and_the_planning_service() -> None:
    files = _files()
    manifest = tomllib.loads(files["deployment.toml"])

    assert manifest["workspace"] == "/research"
    assert manifest["state_root"] == f"/var/lib/heartwood/{manifest['project_id']}"
    assert manifest["identity"] == {
        "issuer": "https://keycloak.example.test/realms/heartwood",
        "client_id": "hw-proj-demo",
        "client_secret_file": "/etc/heartwood/deployment/client-secret",
        "subject": "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001",
        "ca_file": "/etc/heartwood/deployment/platform-ca.crt",
    }
    worker = manifest["worker"]
    assert worker["driver"] == "openshell-kubernetes"
    assert worker["namespace"] == "hw-s-abc123"
    assert worker["image_repository"] == "registry.example.test:5000/heartwood-worker"
    assert worker["image_digest"] == "sha256:" + "2" * 64
    assert worker["supervisor_image_digest"].startswith("sha256:")
    assert worker["sandbox_image_digest"].startswith("sha256:")
    assert (
        worker["policy_file_sha256"]
        == hashlib.sha256(files["worker-policy.json"].encode()).hexdigest()
    )
    assert worker["inputs"] == {"schema_version": "heartwood.worker-inputs.v1", "files": []}
    # The model route names an address that does not resolve, and the policy allows only it.
    assert worker["model_endpoint"] in manifest["policy"]["allowed_model_endpoints"]
    assert manifest["planning"] == {
        "url": "https://heartwood-control.heartwood-platform.svc.cluster.local:8443/mcp",
        "ca_file": "/etc/heartwood/deployment/platform-ca.crt",
        "private_address": True,
        "launch_context_file": "/etc/heartwood/deployment/launch-context.json",
        "token_exchange": {"scope": "svc-control", "audience": "heartwood-control"},
    }
    assert PlatformLaunchContext.model_validate_json(files["launch-context.json"]) == _LAUNCH
    kubeconfig = json.loads(files["kubeconfig"])
    assert kubeconfig["contexts"][0]["context"]["namespace"] == "hw-s-abc123"
    assert "token" not in kubeconfig["users"][0]["user"]


def test_a_session_keeps_its_project_identity_and_sessions_differ() -> None:
    first = tomllib.loads(_files()["deployment.toml"])["project_id"]
    assert tomllib.loads(_files()["deployment.toml"])["project_id"] == first
    assert tomllib.loads(_files("def456")["deployment.toml"])["project_id"] != first


def test_the_files_are_the_ones_the_manifest_names() -> None:
    # The client secret and the certificates come from the launch, not from these files.
    assert set(_files()) == {
        "deployment.toml",
        "kubeconfig",
        "worker-policy.json",
        "launch-context.json",
    }
