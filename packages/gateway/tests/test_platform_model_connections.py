# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deployment-supplied model connections and declarations, exercised against the inference fake.

The deployment manifest supplies the platform connection and its model declarations.
Discovery, selection and an agent turn run through the gateway and the real OpenHands
backend, and discovery failures carry the failure-state diagnostics of the platform contract.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import tomli_w

import heartwood.gateway._openhands_sdk as openhands_sdk_module
from heartwood.cli import main as terminal_main
from heartwood.cli._models import _handle_models, configure_models
from heartwood.core_adapter import SessionService
from heartwood.gateway import (
    ModelCatalogError,
    OpenHandsSdkBackend,
    ProjectStateError,
    RestGateway,
    RestRequest,
    SessionGateway,
    load_project_deployment,
)
from heartwood.gateway._deployment import ProjectDeployment
from heartwood.gateway.platform_fakes import INFERENCE_API_KEY, FakeInference, serve
from heartwood.notebook import NotebookSession
from heartwood.session import CommandKind, EventKind, SessionCommand
from packages.gateway.tests.tls_fixtures import certificate_authority, serve_tls

_ROOT = Path(__file__).resolve().parents[3]
_STATES = json.loads(
    (_ROOT / "fixtures/synthetic/platform-contract/failure-states.json").read_text("utf-8")
)["states"]
_KEY = "HEARTWOOD_SYNTHETIC_PLATFORM_KEY"
_CONNECTION = "synthetic-platform"
_MODELS = ("synthetic-alias", "synthetic-open", "synthetic-responses", "synthetic-unsupported")


@pytest.fixture(autouse=True)
def _fast_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_LOCAL_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_RETRY_MIN_WAIT_SECONDS", 0)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_RETRY_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_PROGRESS_POLL_SECONDS", 0.01)


@pytest.fixture
def inference() -> Iterator[tuple[FakeInference, str]]:
    fake = FakeInference(models=_MODELS)
    with serve(fake.app) as origin:
        yield fake, f"{origin}/v1"


def _record(tmp_path: Path, base_url: str) -> dict[str, object]:
    workspace = tmp_path / "research"
    workspace.mkdir(exist_ok=True)
    state_parent = tmp_path / "protected"
    state_parent.mkdir(mode=0o700, exist_ok=True)
    identity = str(uuid4())
    return {
        "schema_version": "heartwood.project-deployment.v1",
        "project_id": identity,
        "workspace": str(workspace),
        "state_root": str(state_parent / identity),
        "policy": {
            "policy_id": "synthetic-platform",
            "platform_id": "generic",
            "allowed_model_endpoints": [f"{base_url}/chat/completions", f"{base_url}/responses"],
            "allowed_model_catalog_endpoints": [f"{base_url}/models"],
            "allowed_capability_tiers": ["supervised", "experimental"],
            "allowed_action_review_modes": ["review-all"],
            "credential_allowlist": [_KEY],
            "allowed_data_destinations": ["institution-managed"],
        },
        "model_connections": {
            "schema_version": "heartwood.model-connections.v1",
            "connections": [
                {
                    "connection_id": _CONNECTION,
                    "label": "Synthetic Platform Models",
                    "protocol": "openai-compatible",
                    "model_prefix": "openai/",
                    "credential_kind": "environment",
                    "api_key_env": _KEY,
                    "base_url": base_url,
                    "catalog_endpoint": f"{base_url}/models",
                    "policy_endpoint": f"{base_url}/chat/completions",
                    "data_handling": {
                        "destination": "institution-managed",
                        "retention_authority": "institution",
                        "route": "Synthetic platform inference service",
                    },
                }
            ],
        },
        "model_metadata": {
            "schema_version": "heartwood.platform-model-metadata.v1",
            "models": [
                {
                    "connection_id": _CONNECTION,
                    "model_id": "synthetic-alias",
                    "protocols": ["chat"],
                    "context_window_tokens": 32_768,
                    "max_input_tokens": 24_576,
                    "max_output_tokens": 1_024,
                    "supports_tool_calls": False,
                },
                {
                    "connection_id": _CONNECTION,
                    "model_id": "synthetic-unsupported",
                    "protocols": ["synthetic-protocol"],
                },
                {
                    "connection_id": _CONNECTION,
                    "model_id": "synthetic-responses",
                    "protocols": ["responses"],
                    "context_window_tokens": 40_960,
                    "max_output_tokens": 1_024,
                },
                {
                    "connection_id": _CONNECTION,
                    "model_id": "synthetic-unlisted",
                    "protocols": ["chat"],
                },
            ],
        },
    }


def _deployment(tmp_path: Path, record: dict[str, object]) -> ProjectDeployment:
    manifest = tmp_path / "deployment.toml"
    manifest.write_text(tomli_w.dumps(record))
    manifest.chmod(0o600)
    return load_project_deployment(
        manifest, project_root=Path(cast(str, record["workspace"])), env={}
    )


def _gateway(deployment: ProjectDeployment, key: str = INFERENCE_API_KEY) -> SessionGateway:
    return SessionGateway(deployment=deployment, env={_KEY: key})


def test_deployment_connection_presents_the_identity_model_list_with_declarations(
    tmp_path: Path, inference: tuple[FakeInference, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _fake, base_url = inference
    gateway = _gateway(_deployment(tmp_path, _record(tmp_path, base_url)))
    try:
        connections = {
            item["connection_id"]: item for item in gateway.model_settings()["connections"]
        }
        catalog = gateway.discover_models(_CONNECTION, refresh=True)
        models = {item["model_id"]: item for item in catalog["models"]}
        assert _cli(gateway, ["refresh", _CONNECTION]) == 0
        terminal = capsys.readouterr().out
        gateway.connect_model(_CONNECTION, "synthetic-alias")
        readiness = {check.check_id: check for check in gateway.deployment_readiness().checks}
    finally:
        gateway.stop()

    platform = connections[_CONNECTION]
    assert platform["group"] == "research-environment"
    assert platform["credential_status"] == "available"
    assert cast(dict[str, object], platform["data_handling"])["destination"] == (
        "institution-managed"
    )
    # A declaration supplements the endpoint's list; it never adds a model to it.
    assert set(models) == set(_MODELS)
    alias = models["synthetic-alias"]
    assert (alias["availability"], alias["reason"]) == ("available", "Declared by the deployment")
    assert (alias["context_window"], alias["supports_tools"]) == (32_768, False)
    # An undeclared alias the OpenHands model table does not know stays unknown.
    undeclared = models["synthetic-open"]
    assert undeclared["availability"] == "experimental"
    assert (undeclared["context_window"], undeclared["supports_tools"]) == (None, None)
    assert models["synthetic-unsupported"]["availability"] == "unsupported"
    assert "Context window: 32,768 tokens; native tool calls: not supported" in terminal
    assert "Context window: unknown; native tool calls: unknown" in terminal
    assert readiness["model-source"].status == "pass"
    assert readiness["configuration-coherence"].status == "pass"


def test_declared_model_runs_a_turn_with_its_declared_protocol_and_limits(
    tmp_path: Path, inference: tuple[FakeInference, str]
) -> None:
    fake, base_url = inference
    deployment = _deployment(tmp_path, _record(tmp_path, base_url))
    gateway = _gateway(deployment)
    try:
        gateway.connect_model(_CONNECTION, "synthetic-alias")
        backend = gateway._backend(
            model_settings=gateway.settings_store.load(),
            selected_model=None,
            session_id="declared-model",
        )
    finally:
        gateway.stop()

    assert isinstance(backend, OpenHandsSdkBackend)
    llm = backend._get_conversation().state.agent.llm
    assert llm.model == "openai/synthetic-alias"
    assert llm.api_mode == "chat"
    assert llm.native_tool_calling is False
    assert (llm.max_input_tokens, llm.max_output_tokens) == (24_576, 1_024)
    service = SessionService.local_default(
        tmp_path / "sessions",
        session_id="declared-model",
        backend=backend,
        policy_profile=deployment.policy,
        model_data_handling=deployment.model_connections[0].data_handling,
        env={_KEY: INFERENCE_API_KEY},
        clock=lambda: "2026-11-18T00:00:00Z",
    )
    service.handle(
        SessionCommand(
            command_id="declared-task",
            session_id="declared-model",
            kind=CommandKind.CHAT,
            actor_id="synthetic-user",
            created_at="2026-11-18T00:00:00Z",
            payload={"prompt": "Return one short synthetic result without using tools."},
        )
    )
    deadline = time.monotonic() + 15
    try:
        while True:
            events = service.replay_events()
            statuses = [
                event.payload.get("status")
                for event in events
                if event.kind == EventKind.AGENT_LIFECYCLE_UPDATED
            ]
            if statuses and statuses[-1] in {"finished", "error"}:
                break
            assert time.monotonic() < deadline, "the declared model turn did not settle"
            time.sleep(0.02)
    finally:
        service.close()

    assert not any(event.kind == EventKind.ERROR_RECORDED for event in events)
    assert [
        event.payload.get("content")
        for event in events
        if event.kind == EventKind.AGENT_MESSAGE_EMITTED
    ] == [fake.reply]


def _cli(gateway: SessionGateway, arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="heartwood models")
    configure_models(parser)
    return _handle_models(parser, gateway, parser.parse_args(arguments))


def _cli_error(
    gateway: SessionGateway, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> str:
    with pytest.raises(SystemExit):
        _cli(gateway, arguments)
    return capsys.readouterr().err


@pytest.mark.parametrize("interactive", [True, False], ids=["menu", "options"])
def test_guided_setup_lists_and_selects_a_deployment_connection(
    tmp_path: Path,
    inference: tuple[FakeInference, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interactive: bool,
) -> None:
    _fake, base_url = inference
    record = _record(tmp_path, base_url)
    deployment = _deployment(tmp_path, record)
    assert deployment.manifest is not None
    monkeypatch.setenv(_KEY, INFERENCE_API_KEY)
    monkeypatch.chdir(cast(str, record["workspace"]))

    def answer(prompt: str) -> str:
        if prompt.startswith("Select ["):
            # The deployment connection is listed after the platform's own model sources.
            return next(
                line.split(".", 1)[0].strip()
                for line in capsys.readouterr().out.splitlines()
                if line.strip().endswith("Synthetic Platform Models")
            )
        return "y" if prompt.startswith("Continue") else "synthetic-alias"

    monkeypatch.setattr("builtins.input", answer)
    options = (
        []
        if interactive
        else ["--model-source", _CONNECTION, "--model-id", "synthetic-alias", "--yes"]
    )

    assert terminal_main(["setup", "--deployment", str(deployment.manifest), *options]) == 0

    gateway = _gateway(deployment)
    try:
        settings = gateway.model_settings()
        readiness = {check.check_id: check for check in gateway.deployment_readiness().checks}
    finally:
        gateway.stop()
    assert settings["model_source"] == _CONNECTION
    assert settings["active_profile"] == _CONNECTION
    assert readiness["model-source"].status == "pass"
    assert "Setup complete." in capsys.readouterr().out


def test_guided_setup_refuses_a_connection_the_deployment_does_not_declare(
    tmp_path: Path,
    inference: tuple[FakeInference, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake, base_url = inference
    record = _record(tmp_path, base_url)
    deployment = _deployment(tmp_path, record)
    assert deployment.manifest is not None
    monkeypatch.chdir(cast(str, record["workspace"]))
    arguments = ["--model-source", "synthetic-other", "--model-id", "synthetic-alias", "--yes"]

    with pytest.raises(SystemExit) as refused:
        terminal_main(["setup", "--deployment", str(deployment.manifest), *arguments])

    assert refused.value.code == 2
    refusal = capsys.readouterr().err.rstrip()
    assert refusal.endswith(
        "HW-SETUP-004: The selected model connection is not offered on this platform"
    )
    assert not deployment.project.config_path.exists()


def test_guided_setup_resumes_a_deployment_connection_selection(
    tmp_path: Path,
    inference: tuple[FakeInference, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake, base_url = inference
    record = _record(tmp_path, base_url)
    deployment = _deployment(tmp_path, record)
    assert deployment.manifest is not None
    monkeypatch.setenv(_KEY, INFERENCE_API_KEY)
    monkeypatch.chdir(cast(str, record["workspace"]))
    gateway = _gateway(deployment)
    try:
        gateway.connect_model(_CONNECTION, "synthetic-alias")
        selected = gateway.model_settings()
    finally:
        gateway.stop()

    def refuse(prompt: str) -> str:
        raise AssertionError(f"setup asked again: {prompt}")

    monkeypatch.setattr("builtins.input", refuse)

    assert terminal_main(["setup", "--deployment", str(deployment.manifest)]) == 0

    gateway = _gateway(deployment)
    try:
        resumed = gateway.model_settings()
    finally:
        gateway.stop()
    assert resumed["model_source"] == _CONNECTION
    assert resumed["profiles"] == selected["profiles"]


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(state, id=state["discovery_failure"])
        for state in _STATES
        if "discovery_failure" in state
    ],
)
def test_discovery_failures_show_the_failure_state_diagnostic(
    tmp_path: Path,
    inference: tuple[FakeInference, str],
    capsys: pytest.CaptureFixture[str],
    state: dict[str, str],
) -> None:
    fake, base_url = inference
    failure = state["discovery_failure"]
    gateway = _gateway(
        _deployment(tmp_path, _record(tmp_path, base_url)),
        key="synthetic-expired-key" if failure == "authentication" else INFERENCE_API_KEY,
    )
    model = {"protocol": "synthetic-unsupported"}.get(failure, "synthetic-alias")
    try:
        if failure == "withdrawn":
            gateway.connect_model(_CONNECTION, model)
            fake.models = tuple(item for item in _MODELS if item != model)
        elif failure == "unavailable":
            fake.available = False
        if failure in {"authentication", "unavailable"}:
            arguments, path = ["refresh", _CONNECTION], "/settings/models/catalog"
            body = {"connection_id": _CONNECTION, "refresh": True}
        else:
            arguments, path = ["connect", _CONNECTION, model], "/settings/models/connect"
            body = {"connection_id": _CONNECTION, "model_id": model}
        terminal = _cli_error(gateway, arguments, capsys)
        browser = RestGateway(gateway).handle(
            RestRequest(method="POST", path=path, body=json.dumps(body))
        )
        notebook = NotebookSession(gateway=gateway)
        notebook_call = (
            partial(notebook.discover_models, _CONNECTION, refresh=True)
            if failure in {"authentication", "unavailable"}
            else partial(notebook.connect_model, _CONNECTION, model)
        )
        with pytest.raises(ModelCatalogError) as caught:
            notebook_call()
        if failure == "withdrawn":
            # Choosing the saved profile again rereads the list as well.
            with pytest.raises(ModelCatalogError) as reselected:
                gateway.select_model_profile(_CONNECTION)
            assert str(reselected.value) == state["diagnostic"]
        settings = gateway.model_settings()
    finally:
        gateway.stop()

    assert state["diagnostic"] in terminal
    assert str(caught.value) == state["diagnostic"]
    assert browser.status_code == 422
    assert f"{browser.body['code']}: {browser.body['error']}" == state["diagnostic"]
    assert INFERENCE_API_KEY not in terminal
    assert INFERENCE_API_KEY not in json.dumps(browser.body)
    # A withdrawn model stays selected, unchanged, until the researcher chooses another.
    if failure == "withdrawn":
        assert settings["active_profile"] == _CONNECTION
        profiles = cast(list[dict[str, object]], settings["profiles"])
        assert profiles[0]["model"] == f"openai/{model}"
    else:
        assert settings["active_profile"] is None


def test_declared_responses_protocol_sets_the_policy_endpoint_and_request_path(
    tmp_path: Path, inference: tuple[FakeInference, str]
) -> None:
    _fake, base_url = inference
    gateway = _gateway(_deployment(tmp_path, _record(tmp_path, base_url)))
    try:
        settings = gateway.connect_model(_CONNECTION, "synthetic-responses")
        validation = gateway.validate_model_profile()
        backend = gateway._backend(
            model_settings=gateway.settings_store.load(),
            selected_model=None,
            session_id="declared-responses",
        )
    finally:
        gateway.stop()

    profiles = cast(list[dict[str, object]], settings["profiles"])
    assert profiles[0]["policy_endpoint"] == f"{base_url}/responses"
    assert validation["policy_decision"]["decision"] == "allow"
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend.configuration_error is None
    llm = backend._get_conversation().state.agent.llm
    assert llm.uses_responses_api()
    # Only a window and an output limit are declared, so input gets the remainder.
    assert (llm.max_input_tokens, llm.max_output_tokens) == (39_936, 1_024)
    assert llm.native_tool_calling is True
    backend.close()


def test_a_saved_profile_for_a_declared_model_keeps_the_declared_request_path(
    tmp_path: Path, inference: tuple[FakeInference, str]
) -> None:
    _fake, base_url = inference
    gateway = _gateway(_deployment(tmp_path, _record(tmp_path, base_url)))
    try:
        gateway.connect_model(_CONNECTION, "synthetic-responses")
        saved = gateway.settings_store.load().profile(_CONNECTION)
        gateway.save_model_profile(
            replace(saved, policy_endpoint=f"{base_url}/chat/completions", description="Saved")
        )
        profile = gateway.settings_store.load().profile(_CONNECTION)
        backend = gateway._backend(
            model_settings=gateway.settings_store.load(),
            selected_model=None,
            session_id="saved-declared",
        )
    finally:
        gateway.stop()

    assert profile.policy_endpoint == f"{base_url}/responses"
    assert isinstance(backend, OpenHandsSdkBackend)
    assert backend.configuration_error is None


@pytest.mark.parametrize("identity", ["Research", "Custom", "custom", "stanford-ai-api-gateway"])
def test_connection_identifiers_must_be_usable_model_sources(tmp_path: Path, identity: str) -> None:
    record = _record(tmp_path, "https://models.example.invalid/v1")
    record.pop("model_metadata")
    connections = cast(dict[str, list[dict[str, object]]], record["model_connections"])
    connections["connections"][0]["connection_id"] = identity
    with pytest.raises(ProjectStateError, match=f"model sources: {identity}"):
        _deployment(tmp_path, record)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"model_metadata": None}, None),
        ({"model_connections": None}, "model metadata requires model connections"),
        (
            {
                "model_connections": {
                    "schema_version": "heartwood.model-connections.v1",
                    "connections": [{"connection_id": "openai"}],
                }
            },
            "invalid model connections manifest",
        ),
        (
            {
                "model_metadata": {
                    "schema_version": "heartwood.platform-model-metadata.v1",
                    "models": [
                        {
                            "connection_id": "elsewhere",
                            "model_id": "synthetic-alias",
                            "protocols": ["chat"],
                        }
                    ],
                }
            },
            "model metadata names undeclared connections: elsewhere",
        ),
        (
            {
                "model_metadata": {
                    "schema_version": "heartwood.platform-model-metadata.v1",
                    "models": [
                        {
                            "connection_id": _CONNECTION,
                            "model_id": "synthetic-alias",
                            "protocols": [],
                        }
                    ],
                }
            },
            "invalid model metadata: models.0.protocols",
        ),
        (
            {
                "model_metadata": {
                    "schema_version": "heartwood.platform-model-metadata.v1",
                    "models": [
                        {"connection_id": _CONNECTION, "model_id": model, "protocols": ["chat"]}
                        for model in ("synthetic-alias", "openai/synthetic-alias")
                    ],
                }
            },
            "model metadata declares one model more than once",
        ),
    ],
)
def test_manifest_model_sections_are_validated_before_launch(
    tmp_path: Path, change: dict[str, object], message: str | None
) -> None:
    record = _record(tmp_path, "https://models.example.invalid/v1")
    for key, value in change.items():
        if value is None:
            record.pop(key)
        else:
            record[key] = value
    if message is None:
        deployment = _deployment(tmp_path, record)
        assert [item.connection_id for item in deployment.model_connections] == [_CONNECTION]
        assert deployment.model_connections[0].model_metadata == ()
        return
    with pytest.raises(ProjectStateError, match=message) as caught:
        _deployment(tmp_path, record)
    assert str(caught.value).startswith("HW-DEPLOY-001: ")


def test_deployment_connections_cannot_be_combined_with_injected_connections(
    tmp_path: Path,
) -> None:
    deployment = _deployment(tmp_path, _record(tmp_path, "https://models.example.invalid/v1"))
    with pytest.raises(ValueError, match="model connections"):
        SessionGateway(deployment=deployment, model_connections=(), env={})


def test_a_changed_model_declaration_stops_dispatch_until_restart(tmp_path: Path) -> None:
    record = _record(tmp_path, "https://models.example.invalid/v1")
    deployment = _deployment(tmp_path, record)
    gateway = SessionGateway(deployment=deployment, env={}, backend_id="deterministic")
    try:
        metadata = cast(dict[str, list[dict[str, object]]], record["model_metadata"])
        metadata["models"][0]["supports_tool_calls"] = True
        manifest = tmp_path / "deployment.toml"
        manifest.write_text(tomli_w.dumps(record))
        assert str(gateway.effective_policy().change_refused).startswith("HW-DEPLOY-004")
    finally:
        gateway.stop()


def test_discovery_trusts_the_connection_certificate_authorities_alone(tmp_path: Path) -> None:
    platform = certificate_authority(tmp_path, "platform")
    other = certificate_authority(tmp_path, "other")
    fake = FakeInference(models=_MODELS)
    with serve_tls(fake.app, tmp_path / "served", platform) as origin:
        base_url = f"{origin}/v1"
        for authority, expected in ((platform, set(_MODELS)), (other, None)):
            directory = tmp_path / authority[0].stem
            directory.mkdir()
            record = _record(directory, base_url)
            connections = cast(dict[str, list[dict[str, object]]], record["model_connections"])
            connections["connections"][0]["ca_file"] = str(authority[0])
            gateway = _gateway(_deployment(directory, record))
            try:
                if expected is None:
                    with pytest.raises(ModelCatalogError, match="HW-AGENT-012"):
                        gateway.discover_models(_CONNECTION, refresh=True)
                else:
                    catalog = gateway.discover_models(_CONNECTION, refresh=True)
                    assert {item["model_id"] for item in catalog["models"]} == expected
            finally:
                gateway.stop()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ca_file": "platform-ca.crt"}, "ca_file is an absolute path"),
        ({"ca_file": "/nonexistent/platform-ca.crt"}, None),
    ],
)
def test_a_connection_certificate_authority_file_is_checked_at_load(
    tmp_path: Path, change: dict[str, object], message: str | None
) -> None:
    record = _record(tmp_path, "https://models.example.invalid/v1")
    connections = cast(dict[str, list[dict[str, object]]], record["model_connections"])
    connections["connections"][0].update(change)
    with pytest.raises(ProjectStateError, match=message) as caught:
        _deployment(tmp_path, record)
    assert str(caught.value).startswith(("HW-DEPLOY-001: ", "HW-DEPLOY-"))
