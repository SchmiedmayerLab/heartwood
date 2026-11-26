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

import pytest
from heartwood_platform.control.hosted import (
    HostedGateway,
    HostedGatewaySettings,
    _table,
    sandbox_digests,
)
from heartwood_platform.control.inference import RouteModel

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
        "policy": {"version": 1, "process": {"runAsUser": "10001"}},
        "cpu": "1",
        "memory": "2Gi",
    },
    "model_route": {
        "profile_id": "platform-route",
        "model": "synthetic-coder",
        "endpoint": "https://litellm.heartwood-platform.svc.cluster.local:4000/v1/chat/completions",
        "provider": "platform-route",
        "placeholder_env": "HEARTWOOD_PLATFORM_MODEL_KEY",
        "binaries": ["/usr/local/bin/python3.12"],
        "data_handling": {
            "destination": "operator-defined",
            "retention_authority": "service-operator",
            "route": "The platform's LiteLLM proxy and the model behind it",
        },
    },
}
MODELS = (RouteModel("synthetic-coder", 32768, 4096, supports_tool_calls=True),)
_LAUNCH = PlatformLaunchContext(
    project_context_id="abc123",
    platform_id="reference-platform",
    project_id="demo",
    billing_attribution_ref="grant",
    authority_ref="reference-platform",
    policy_ref="policy",
    issued_at=datetime(2026, 11, 19, tzinfo=UTC),
)


def _files(
    session: str = "abc123", subject: str = "6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001"
) -> dict[str, str]:
    supervisor, sandbox = sandbox_digests(_OBJECTS.read_text(encoding="utf-8"))
    gateway = HostedGateway(
        HostedGatewaySettings.model_validate(HOSTED),
        issuer="https://keycloak.example.test/realms/heartwood",
        supervisor=supervisor,
        sandbox=sandbox,
        models=MODELS,
    )
    return gateway.files(
        session=session,
        subject=subject,
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
        "actor_key_file": "/etc/heartwood/deployment/actor-key",
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
    # The worker calls the route's model through LiteLLM, and the policy allows only that.
    endpoint = "https://litellm.heartwood-platform.svc.cluster.local:4000/v1"
    assert (worker["profile_id"], worker["model"]) == ("platform-route", "openai/synthetic-coder")
    assert worker["model_endpoint"] == f"{endpoint}/chat/completions"
    assert worker["provider"] == "platform-route"
    policy = manifest["policy"]
    assert policy["allowed_model_endpoints"] == [f"{endpoint}/chat/completions"]
    assert policy["allowed_model_catalog_endpoints"] == [f"{endpoint}/models"]
    assert policy["allowed_data_destinations"] == ["operator-defined"]
    # The gateway lists the models with the session's discovery key and the platform CA.
    (connection,) = manifest["model_connections"]["connections"]
    assert connection["connection_id"] == worker["profile_id"]
    assert connection["api_key_file"] == "/etc/heartwood/deployment/model-discovery-key"
    assert policy["credential_allowlist"] == [connection["api_key_file"]]
    assert connection["ca_file"] == "/etc/heartwood/deployment/platform-ca.crt"
    assert (connection["catalog_endpoint"], connection["policy_endpoint"]) == (
        f"{endpoint}/models",
        f"{endpoint}/chat/completions",
    )
    assert connection["data_handling"]["destination"] == "operator-defined"
    (declared,) = manifest["model_metadata"]["models"]
    assert declared == {
        "connection_id": "platform-route",
        "model_id": "synthetic-coder",
        "protocols": ["chat"],
        "context_window_tokens": 36864,
        "max_input_tokens": 32768,
        "max_output_tokens": 4096,
        "supports_tool_calls": True,
    }
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


def test_a_worker_image_without_a_registry_path_keeps_its_name() -> None:
    settings = dict(HOSTED, worker={**HOSTED["worker"], "image": "worker:1@sha256:" + "2" * 64})
    supervisor, sandbox = sandbox_digests(_OBJECTS.read_text(encoding="utf-8"))
    gateway = HostedGateway(
        HostedGatewaySettings.model_validate(settings),
        issuer="https://keycloak.example.test/realms/heartwood",
        supervisor=supervisor,
        sandbox=sandbox,
        models=MODELS,
    )
    files = gateway.files(
        session="abc123",
        subject="6d1f7c0a-51a4-4d0b-9c1e-0c5a6e2f0001",
        client_id="hw-proj-demo",
        namespace="hw-s-abc123",
        launch_context=_LAUNCH,
    )
    assert tomllib.loads(files["deployment.toml"])["worker"]["image_repository"] == "worker"


@pytest.mark.parametrize(
    "subject",
    [
        "auth0|64ab12cd34ef56ab78cd90ef",
        "synthetic@example.test",
        "r\u00e9searcher \U0001f600 \U00020000",
        'quote " and backslash \\',
    ],
)
def test_the_manifest_binds_a_subject_in_any_form_exactly(subject: str) -> None:
    manifest = tomllib.loads(_files(subject=subject)["deployment.toml"])

    assert manifest["identity"]["subject"] == subject


def test_the_manifest_writer_escapes_what_toml_requires() -> None:
    value = "".join(chr(code) for code in (*range(0x20), 0x7F, 0x85)) + "\U0001f600"

    parsed = tomllib.loads(_table("", {"text": value, "list": [value, "plain"]}))

    assert parsed == {"text": value, "list": [value, "plain"]}
