# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Contract inventory, schema composition, and real transport dispatch checks."""

from __future__ import annotations

import ast
import inspect
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._openapi import _REST_OPERATIONS, _Operation, gateway_openapi
from heartwood.gateway._rest import RestGateway, RestRequest

# An independent behavioral oracle: each advertised route must reach its actual owner.
_GATEWAY_METHODS = {
    ("GET", "/sessions"): "sessions",
    ("POST", "/sessions"): "create_session",
    ("POST", "/sessions/default"): "default_session",
    ("GET", "/project/readiness"): "project_readiness",
    ("GET", "/project/capabilities"): "platform_capabilities",
    ("GET", "/project/startup"): "startup_plan",
    ("POST", "/project/initialize"): "initialize_project",
    ("GET", "/sessions/{session_id}"): "session",
    ("PATCH", "/sessions/{session_id}"): "rename_session",
    ("GET", "/settings/actions"): "action_settings",
    ("PUT", "/settings/actions/confirmation"): "select_action_confirmation_mode",
    ("GET", "/settings/models"): "model_settings",
    ("GET", "/settings/credentials"): "credential_settings",
    ("DELETE", "/settings/credentials/{connection_id}"): "forget_credential",
    ("GET", "/settings/skills"): "skill_settings",
    ("GET", "/research/environment"): "verification_environment",
    ("GET", "/research/workflows"): "research_workflows",
    ("GET", "/research/experiments"): "experiment_records",
    ("GET", "/research/experiments/export"): "export_experiments",
    ("GET", "/settings/specialists"): "specialist_settings",
    ("POST", "/settings/skills/inspect"): "inspect_skill",
    ("POST", "/settings/skills/install"): "install_skill",
    ("POST", "/settings/skills/refresh"): "refresh_skills",
    ("POST", "/settings/skills/local/inspect"): "inspect_local_skill",
    ("POST", "/settings/skills/local/install"): "install_local_skill",
    ("DELETE", "/settings/skills/{name}"): "remove_skill",
    ("GET", "/settings/models/artifacts"): "model_artifacts",
    ("POST", "/settings/models/catalog"): "discover_models",
    ("POST", "/settings/models/subscription/device"): "start_subscription_device_login",
    ("POST", "/settings/models/subscription/device/poll"): "poll_subscription_device_login",
    ("POST", "/settings/models/repository"): "inspect_model_repository",
    ("PUT", "/settings/models/source"): "configure_model_source",
    ("POST", "/settings/models/downloads"): "download_local_model",
    ("POST", "/settings/models/downloads/custom"): "download_custom_local_model",
    ("POST", "/settings/models/imports"): "import_local_model",
    ("POST", "/settings/models/transfers/inspect"): "inspect_local_model_bundle",
    ("POST", "/settings/models/transfers/exports"): "export_local_model",
    ("POST", "/settings/models/transfers/imports"): "import_local_model_bundle",
    ("DELETE", "/settings/models/transfers/{transfer_id}"): "cancel_model_transfer",
    ("GET", "/settings/models/validation"): "validate_model_profile",
    ("POST", "/settings/models/profiles"): "save_model_profile",
    ("POST", "/settings/models/connect"): "connect_model",
    ("PUT", "/settings/models/active"): "select_model_profile",
    ("DELETE", "/settings/models/profiles/{profile_id}"): "remove_model_profile",
    ("GET", "/sessions/{session_id}/workspace/tree"): "workspace_tree",
    ("GET", "/sessions/{session_id}/workspace/file"): "workspace_file",
    ("GET", "/sessions/{session_id}/workspace/changes"): "workspace_changes",
    ("GET", "/sessions/{session_id}/workspace/diff"): "workspace_diff",
    ("GET", "/sessions/{session_id}/audit-export"): "audit_export",
    ("POST", "/sessions/{session_id}/commands"): "handle",
    ("GET", "/sessions/{session_id}/projection"): "session_projection",
    ("GET", "/sessions/{session_id}/events"): "session_snapshot",
}
_REQUEST_EXAMPLES: dict[str, dict[str, Any]] = {
    "SessionCreateRequest": {},
    "SessionRenameRequest": {"title": "Synthetic study"},
    "ActionConfirmationRequest": {"mode": "always-confirm"},
    "ModelDownloadRequest": {"model_id": "synthetic"},
    "ModelRepositoryRequest": {"repository": "synthetic/model"},
    "CustomLocalModelDownloadRequest": {"repository": "synthetic/model"},
    "LocalModelImportRequest": {
        "path": "weights",
        "repository": "synthetic/model",
        "revision": "revision",
        "license": "MIT",
    },
    "ModelTransferInspectRequest": {"path": "bundle.zip"},
    "ModelTransferExportRequest": {"path": "bundle.zip"},
    "ModelTransferImportRequest": {
        "path": "bundle.zip",
        "approved": True,
        "manifest_sha256": "a" * 64,
    },
    "ModelSourceRequest": {"source_id": "custom"},
    "ModelCatalogRequest": {"connection_id": "synthetic"},
    "ModelConnectRequest": {"connection_id": "synthetic", "model_id": "model"},
    "ModelProfileRequest": {
        "profile_id": "synthetic",
        "model": "openai/synthetic",
        "policy_endpoint": "http://127.0.0.1:8765/v1/chat/completions",
        "credential_kind": "none",
    },
    "SubscriptionDeviceLoginRequest": {"connection_id": "synthetic", "terms_accepted": True},
    "SubscriptionDevicePollRequest": {"connection_id": "synthetic", "login_id": "synthetic"},
    "ModelSelectionRequest": {"profile_id": "synthetic"},
    "SkillInspectRequest": {"name": "synthetic"},
    "SkillInstallRequest": {
        "name": "synthetic",
        "approved": True,
        "expected_tree_sha256": "a" * 64,
    },
    "SkillLocalInspectRequest": {"source": "skills/synthetic"},
    "SkillLocalInstallRequest": {
        "source": "skills/synthetic",
        "approved": True,
        "expected_tree_sha256": "a" * 64,
    },
    "SkillRefreshRequest": {},
    "SessionCommand": {
        "command_id": "synthetic",
        "session_id": "synthetic",
        "kind": "pause",
        "created_at": "2026-01-01T00:00:00Z",
    },
}


class _FakeResponse(dict[str, Any]):
    events: tuple[()] = ()

    @property
    def projection(self) -> _FakeResponse:
        return self

    def safe_dict(self) -> dict[str, Any]:
        return dict(self)

    def model_dump(self, **_kwargs: object) -> dict[str, Any]:
        return dict(self)


class _RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., _FakeResponse]:
        def call(*_args: object, **_kwargs: object) -> _FakeResponse:
            self.calls.append(name)
            return _FakeResponse()

        return call


@pytest.mark.parametrize(
    "operation", _REST_OPERATIONS, ids=lambda item: f"{item.method} {item.path}"
)
def test_documented_operation_reaches_its_gateway_owner(operation: _Operation) -> None:
    gateway = _RecordingGateway()
    body = (
        ""
        if operation.request is None
        else json.dumps(_REQUEST_EXAMPLES[operation.request.__name__])
    )
    path = re.sub(r"{[^}]+}", "synthetic", operation.path)
    if path.endswith(("/workspace/file", "/workspace/diff")):
        path += "?path=synthetic.txt"
    response = RestGateway(cast(SessionGateway, gateway)).handle(
        RestRequest(operation.method, path, body)
    )
    assert response.status_code == operation.status, response.body
    assert _GATEWAY_METHODS[operation.method, operation.path] in gateway.calls


def test_inventory_covers_runtime_static_routes_and_behavioral_cases() -> None:
    assert {(item.method, item.path) for item in _REST_OPERATIONS} == set(_GATEWAY_METHODS)
    # Discover literal paths from the real dispatch comparisons, not a second route table.
    # Dynamic paths are covered by the behavioral cases above and their source branches below.
    module = inspect.getmodule(RestGateway)
    assert module is not None
    source = ast.parse(inspect.getsource(module))
    documented = {item.path for item in _REST_OPERATIONS}
    for node in ast.walk(source):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "parts":
            continue
        for comparison in node.comparators:
            candidates = comparison.elts if isinstance(comparison, ast.Set) else [comparison]
            for candidate in candidates:
                if isinstance(candidate, ast.Tuple) and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in candidate.elts
                ):
                    assert "/" + "/".join(ast.literal_eval(candidate)) in documented
    resources = {"audit-export", "commands", "projection", "events"}
    actual = {
        node.comparators[0].value
        for node in ast.walk(source)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "resource"
        and isinstance(node.comparators[0], ast.Constant)
    }
    assert actual == resources


def _references(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def test_openapi_resolves_references_and_declares_path_parameters() -> None:
    document = gateway_openapi(api_version="synthetic")
    assert document["openapi"] == "3.1.0"
    operation_ids = []
    for path, methods in document["paths"].items():
        for operation in methods.values():
            operation_ids.append(operation["operationId"])
            declared = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "path" and parameter["required"]
            }
            assert declared == set(re.findall(r"{([^}]+)}", path))
    assert len(operation_ids) == len(set(operation_ids)) == 54
    for schema in document["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)
    for reference in _references(document):
        assert reference.startswith("#/components/schemas/")
        assert reference.removeprefix("#/components/schemas/") in document["components"]["schemas"]


def test_security_streaming_and_schema_modes_describe_the_current_transport() -> None:
    document = gateway_openapi(api_version="synthetic")
    assert document["security"] == [{"CapabilityHeader": []}, {"CapabilityCookie": []}]
    assert document["paths"]["/launch"]["get"]["security"] == []
    assert "303" in document["paths"]["/launch"]["get"]["responses"]
    command = document["paths"]["/sessions/{session_id}/commands"]["post"]
    snapshot = command["responses"]["200"]["content"]["application/json"]["schema"]
    stream = document["paths"]["/sessions/{session_id}/events/stream"]["get"]
    assert stream["x-sse-event"]["dataSchema"] == snapshot
    assert document["x-websocket"]["messageSchema"] == snapshot
    assert document["x-websocket"]["rejectedCloseCode"] == 1008
    assert "409" not in stream["responses"]  # Backend opens after the stream begins.
    create = document["paths"]["/sessions"]["post"]
    assert create["requestBody"]["required"] is False
    assert command["requestBody"]["required"] is True
    request_ref = create["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request_schema = document["components"]["schemas"][request_ref.rsplit("/", 1)[1]]
    assert request_schema["additionalProperties"] is False
    assert "#154" in document["info"]["description"]
    assert "#158" in command["description"]


def test_generation_is_deterministic_and_returns_independent_documents() -> None:
    first = gateway_openapi(api_version="synthetic")
    second = gateway_openapi(api_version="synthetic")
    assert first == second
    first["security"][0]["CapabilityHeader"].append("changed")
    assert second == gateway_openapi(api_version="synthetic")


@pytest.mark.parametrize(
    "path",
    [
        "/sessions",
        "/project/capabilities",
        "/research/workflows",
        "/sessions/synthetic/projection",
        "/sessions/synthetic/events",
    ],
)
def test_real_gateway_responses_match_exported_owning_types(tmp_path: Path, path: str) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    try:
        response = RestGateway(gateway).handle(RestRequest("GET", path))
        assert response.status_code == 200
        template = path.replace("synthetic", "{session_id}")
        document = gateway_openapi(api_version="synthetic")
        schema = document["paths"][template]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        validator = Draft202012Validator({**schema, "components": document["components"]})
        validator.validate(response.body)
    finally:
        gateway.stop()


def test_exporter_writes_only_the_generated_document() -> None:
    script = Path(__file__).parents[1] / "scripts" / "export_openapi.py"
    result = subprocess.run(
        [sys.executable, str(script)], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == gateway_openapi()


@pytest.mark.parametrize(
    "operation",
    [item for item in _REST_OPERATIONS if item.request is not None],
    ids=lambda item: f"{item.method} {item.path}",
)
def test_request_examples_and_invalid_bodies_agree_with_contract(operation: _Operation) -> None:
    document = gateway_openapi(api_version="synthetic")
    definition = document["paths"][operation.path][operation.method.lower()]
    schema = definition["requestBody"]["content"]["application/json"]["schema"]
    validator = Draft202012Validator({**schema, "components": document["components"]})
    example = _REQUEST_EXAMPLES[operation.request.__name__]
    validator.validate(example)
    extra = {**example, "unexpected": "refuse"}
    assert not validator.is_valid(extra)
    path = re.sub(r"{[^}]+}", "synthetic", operation.path)
    gateway = _RecordingGateway()
    rest = RestGateway(cast(SessionGateway, gateway))
    for body, expected in [("{", 400), (json.dumps(extra), 422)]:
        response = rest.handle(RestRequest(operation.method, path, body))
        assert response.status_code == expected
        assert str(expected) in definition["responses"]
    assert gateway.calls == []
