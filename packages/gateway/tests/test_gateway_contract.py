# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for gateway command handling and replayable event streams."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from heartwood.gateway import (
    LocalModelChoice,
    LocalModelDownloadPlan,
    ModelArtifact,
    ModelArtifactCatalog,
    ModelCatalogService,
    ModelRepositoryError,
    ModelSnapshot,
    ProjectContext,
    ProviderModel,
    RestGateway,
    RestRequest,
    RestResponse,
    SessionGateway,
)
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


def _command(kind: CommandKind, *, session_id: str = "session-1", **payload: JsonValue) -> str:
    command = SessionCommand(
        command_id=f"{session_id}-{kind.value}",
        session_id=session_id,
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )
    return command.model_dump_json()


def _events(response: RestResponse) -> list[Mapping[str, JsonValue]]:
    events = response.body["events"]
    assert isinstance(events, list)
    assert all(isinstance(event, dict) for event in events)
    return cast(list[Mapping[str, JsonValue]], events)


def _gateway(workspace: Path) -> SessionGateway:
    workspace.mkdir(parents=True, exist_ok=True)
    return SessionGateway(
        project=ProjectContext(workspace),
        env={},
        backend_id="deterministic",
    )


def test_projects_isolate_configuration_sessions_and_artifacts(tmp_path: Path) -> None:
    first = _gateway(tmp_path / "first-project")
    second = _gateway(tmp_path / "second-project")

    first_session = first.create_session("First project session")
    first.select_action_confirmation_mode("confirm-risky")
    (first.project.models_dir / "project-marker").write_text("first\n", encoding="utf-8")

    assert first.sessions() == {"sessions": [first_session]}
    assert second.sessions() == {"sessions": []}
    assert first.action_settings()["confirmation_mode"] == "confirm-risky"
    assert second.action_settings()["confirmation_mode"] == "always-confirm"
    assert not (second.project.models_dir / "project-marker").exists()
    assert first.project.config_path != second.project.config_path


def test_rest_command_routes_through_session_service_and_streams_events(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    stream = gateway.websocket(session_id="session-1")
    rest = RestGateway(gateway)

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT),
        )
    )

    assert response.status_code == 200
    assert [event["kind"] for event in _events(response)] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]
    assert [event.kind for event in stream.receive()] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]


def test_rest_event_replay_supports_reconnect_after_sequence(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.CHAT, prompt="summarize"),
        )
    )

    replay = rest.handle(RestRequest(method="GET", path="/sessions/session-1/events?after=0"))
    stream = gateway.websocket(session_id="session-1", after_sequence=0)

    assert replay.status_code == 200
    assert [event["sequence"] for event in _events(replay)] == [1, 2, 3, 4, 5]
    assert [event.sequence for event in stream.receive()] == [1, 2, 3, 4, 5]


def test_rest_rejects_invalid_session_id_before_accessing_state(tmp_path: Path) -> None:
    response = RestGateway(_gateway(tmp_path)).handle(
        RestRequest(method="GET", path="/sessions/invalid!session/events")
    )

    assert response.status_code == 422
    assert response.body == {
        "error": (
            "session id must start with a letter or number and contain at most 128 "
            "letters, numbers, dots, hyphens, or underscores"
        )
    }
    assert list((tmp_path / ".heartwood" / "sessions").iterdir()) == []


def test_rest_command_persists_gateway_audit_log(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT),
        )
    )

    audit_lines = (
        (tmp_path / ".heartwood" / "sessions" / "session-1" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert response.status_code == 200
    assert len(audit_lines) == 2


def test_rest_delivers_generated_scrubbed_audit_export(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))

    missing = rest.handle(RestRequest(method="GET", path="/sessions/missing/audit-export"))
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT),
        )
    )
    unavailable = rest.handle(RestRequest(method="GET", path="/sessions/session-1/audit-export"))
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.AUDIT_EXPORT),
        )
    )
    exported = rest.handle(RestRequest(method="GET", path="/sessions/session-1/audit-export"))

    assert missing == RestResponse(status_code=404, body={"error": "unknown session: missing"})
    assert unavailable == RestResponse(
        status_code=404,
        body={"error": "audit export is not available for session: session-1"},
    )
    assert exported.status_code == 200
    assert exported.body["filename"] == "session-1-audit.jsonl"
    assert "audit.export.recorded" in str(exported.body["content"])


def test_pause_and_resume_are_streamed(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    stream = gateway.websocket(session_id="session-1")
    rest = RestGateway(gateway)

    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.PAUSE),
        )
    )
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.RESUME),
        )
    )

    assert [event.kind for event in stream.receive()] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_PAUSED.value,
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_RESUMED.value,
    ]


def test_task_streams_confirmation_request_after_route_authorization(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(
                CommandKind.RUN,
                prompt="run",
            ),
        )
    )

    assert response.status_code == 200
    assert EventKind.CONFIRMATION_REQUESTED.value in [event["kind"] for event in _events(response)]


def test_unconfigured_model_fails_without_allowing_a_synthetic_route(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    rest = RestGateway(gateway)

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.CHAT, prompt="summarize"),
        )
    )

    assert response.status_code == 200
    events = _events(response)
    assert [event["kind"] for event in events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert "no active model profile" in str(events[-1]["payload"])


def test_allow_once_streams_confirmation_resolution_and_execution(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(
                CommandKind.RUN,
                prompt="run",
            ),
        )
    )

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(
                CommandKind.APPROVE,
                target_type="tool-call",
                target_id="session-1-toolcall-0",
            ),
        )
    )

    assert response.status_code == 200
    kinds = [event["kind"] for event in _events(response)]
    assert EventKind.CONFIRMATION_RESOLVED.value in kinds
    assert EventKind.TOOL_EXECUTION_RECORDED.value in kinds


def test_rest_rejects_malformed_and_mismatched_commands(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))

    malformed = rest.handle(
        RestRequest(method="POST", path="/sessions/session-1/commands", body="{")
    )
    invalid = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=json.dumps({"session_id": "session-1"}),
        )
    )
    mismatched = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT, session_id="other-session"),
        )
    )

    assert malformed.status_code == 400
    assert invalid.status_code == 422
    assert mismatched.status_code == 409


def test_rest_rejects_unknown_routes_and_methods(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))

    assert rest.handle(RestRequest(method="GET", path="/unknown")).status_code == 404
    method_response = rest.handle(RestRequest(method="PUT", path="/sessions/session-1/events"))
    assert method_response.status_code == 405


def test_rest_rejects_invalid_replay_cursor(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))

    response = rest.handle(
        RestRequest(method="GET", path="/sessions/session-1/events?after=latest")
    )

    assert response.status_code == 400


def test_rest_reads_and_selects_action_confirmation_mode(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path / "sessions"))

    initial = rest.handle(RestRequest(method="GET", path="/settings/actions"))
    selected = rest.handle(
        RestRequest(
            method="PUT",
            path="/settings/actions/confirmation",
            body=json.dumps({"mode": "confirm-risky"}),
        )
    )

    assert initial.status_code == 200
    assert initial.body["confirmation_mode"] == "always-confirm"
    assert selected.status_code == 200
    assert selected.body["confirmation_mode"] == "confirm-risky"


def test_rest_rejects_malformed_action_confirmation_selection(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path / "sessions"))

    responses = [
        rest.handle(RestRequest(method="PUT", path="/settings/actions/confirmation", body="{")),
        rest.handle(RestRequest(method="PUT", path="/settings/actions/confirmation", body="{}")),
        rest.handle(
            RestRequest(
                method="PUT",
                path="/settings/actions/confirmation",
                body=json.dumps({"mode": "never-confirm"}),
            )
        ),
    ]

    assert [response.status_code for response in responses] == [400, 422, 422]


def test_rest_reports_malformed_persisted_settings(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    (tmp_path / ".heartwood" / "config.toml").write_text("{", encoding="utf-8")
    rest = RestGateway(gateway)

    action_response = rest.handle(RestRequest(method="GET", path="/settings/actions"))
    model_response = rest.handle(RestRequest(method="GET", path="/settings/models"))

    assert action_response.status_code == 422
    assert model_response.status_code == 422
    assert "unable to load" in str(action_response.body["error"])
    assert "unable to load" in str(model_response.body["error"])


def test_rest_manages_model_profiles_and_artifact_metadata(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)
    profile = {
        "profile_id": "local",
        "model": "openai/local-model",
        "policy_endpoint": "http://127.0.0.1:8765/v1/chat/completions",
        "capability_tier": "supervised",
        "base_url": "http://127.0.0.1:8765/v1",
        "credential_kind": "none",
        "api_key_env": None,
        "api_key_file": None,
        "api_version": None,
        "aws_region_name": None,
        "aws_profile_name": None,
        "description": "Local fixture",
    }

    initial = rest.handle(RestRequest(method="GET", path="/settings/models"))
    saved = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/profiles",
            body=json.dumps(profile),
        )
    )
    selected = rest.handle(
        RestRequest(
            method="PUT",
            path="/settings/models/active",
            body=json.dumps({"profile_id": "local"}),
        )
    )
    validated = rest.handle(
        RestRequest(method="GET", path="/settings/models/validation?profile_id=local")
    )
    artifacts = rest.handle(RestRequest(method="GET", path="/settings/models/artifacts"))
    removed = rest.handle(RestRequest(method="DELETE", path="/settings/models/profiles/local"))
    assert initial.status_code == 200
    assert initial.body["model_source"] is None
    source_options = cast(list[dict[str, JsonValue]], initial.body["source_options"])
    assert [option["source_id"] for option in source_options] == [
        "local",
        "openai",
        "anthropic",
        "stanford-ai-api-gateway",
    ]
    connections = cast(list[dict[str, JsonValue]], initial.body["connections"])
    assert [connection["connection_id"] for connection in connections] == [
        "local",
        "openai",
        "anthropic",
        "custom-api",
    ]
    assert all(
        "token" not in connection and "api_key" not in connection for connection in connections
    )
    assert saved.status_code == 200
    assert selected.body["active_profile"] == "local"
    assert validated.status_code == 200
    assert artifacts.status_code == 200
    assert artifacts.body["schema_version"] == "heartwood.local-model-catalog.v1"
    assert artifacts.body["snapshot_schema_version"] == "heartwood.model-snapshot-catalog.v1"
    assert artifacts.body["snapshots"]
    assert removed.body["active_profile"] is None
    artifact_ids = {
        artifact["artifact_id"]
        for artifact in cast(list[dict[str, JsonValue]], artifacts.body["artifacts"])
    }
    assert artifact_ids == {
        "llama-cpp-stories260k-ci",
        "qwen25-7b-instruct-q4_k_m",
        "qwen25-coder-7b-instruct-q4_k_m",
    }


def test_local_model_availability_reflects_installed_runtime_executables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    real_is_file = Path.is_file

    def without_packaged_runtimes(path: Path) -> bool:
        if path in {
            Path("/opt/heartwood-vllm/bin/vllm"),
            Path("/opt/llama.cpp/llama-server"),
        }:
            return False
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", without_packaged_runtimes)
    monkeypatch.setattr("heartwood.gateway._gateway.shutil.which", lambda *_args, **_kwargs: None)

    unavailable = cast(list[dict[str, JsonValue]], gateway.model_artifacts()["models"])
    assert not any(model["available"] for model in unavailable)
    rejected = RestGateway(gateway).handle(
        RestRequest(
            method="POST",
            path="/settings/models/downloads",
            body=json.dumps({"model_id": "qwen25-7b-instruct-q4_k_m"}),
        )
    )
    assert rejected.status_code == 422
    assert "portable CPU runtime" in str(rejected.body["error"])

    monkeypatch.setattr(
        "heartwood.gateway._gateway.shutil.which",
        lambda executable, **_kwargs: (
            "/runtime/llama-server" if executable == "llama-server" else None
        ),
    )
    gateway.env["PATH"] = "/runtime"
    available = cast(list[dict[str, JsonValue]], gateway.model_artifacts()["models"])
    assert all(model["available"] is (model["runtime"] == "llama-cpp") for model in available)

    monkeypatch.setattr(
        "heartwood.gateway._gateway.shutil.which",
        lambda executable, **_kwargs: f"/runtime/{executable}",
    )
    gateway.env["CUDA_VISIBLE_DEVICES"] = "0"
    fully_available = cast(list[dict[str, JsonValue]], gateway.model_artifacts()["models"])
    assert all(model["available"] for model in fully_available)


def test_rest_discovers_and_connects_a_normalized_model_catalog(tmp_path: Path) -> None:
    service = ModelCatalogService(
        openai_lister=lambda _connection, _api_key: (ProviderModel("local-coder", "Local Coder"),),
        compatibility=lambda _connection, _model: (
            "available",
            "Verified by the pinned OpenHands SDK",
            32_768,
            True,
        ),
    )
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
        model_catalog_service=service,
    )
    rest = RestGateway(gateway)

    catalog = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/catalog",
            body=json.dumps({"connection_id": "local", "refresh": True}),
        )
    )
    connected = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/connect",
            body=json.dumps({"connection_id": "local", "model_id": "local-coder"}),
        )
    )

    assert catalog.status_code == 200
    assert catalog.body["models"] == [
        {
            "model_id": "local-coder",
            "display_name": "Local Coder",
            "execution_model": "openai/local-coder",
            "availability": "available",
            "reason": "Verified by the pinned OpenHands SDK",
            "context_window": 32_768,
            "supports_tools": True,
        }
    ]
    assert connected.status_code == 200
    assert connected.body["active_profile"] == "local"
    profiles = cast(list[dict[str, JsonValue]], connected.body["profiles"])
    assert profiles[0]["model"] == "openai/local-coder"
    assert gateway.config_store.load().model_source == "local"
    restarted = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    assert restarted.model_settings()["active_profile"] == "local"
    assert restarted.config_store.load().model_source == "local"
    switched = RestGateway(restarted).handle(
        RestRequest(
            method="PUT",
            path="/settings/models/source",
            body=json.dumps({"source_id": "openai"}),
        )
    )
    assert switched.status_code == 200
    assert switched.body["active_profile"] is None
    assert switched.body["model_source"] == "openai"
    assert restarted.project_readiness()["state"] == "setup-required"
    assert any(
        profile["profile_id"] == "local"
        for profile in cast(list[dict[str, JsonValue]], switched.body["profiles"])
    )


def test_rest_shares_project_readiness_and_model_source_setup(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)

    initial = rest.handle(RestRequest(method="GET", path="/project/readiness"))
    configured = rest.handle(
        RestRequest(
            method="PUT",
            path="/settings/models/source",
            body=json.dumps({"source_id": "stanford-ai-api-gateway"}),
        )
    )
    readiness = rest.handle(RestRequest(method="GET", path="/project/readiness"))

    assert initial.status_code == 200
    assert initial.body["state"] == "setup-required"
    assert initial.body["project_root"] == str(tmp_path)
    assert configured.status_code == 200
    assert configured.body["model_source"] == "stanford-ai-api-gateway"
    connections = cast(list[dict[str, JsonValue]], configured.body["connections"])
    assert any(
        connection["connection_id"] == "stanford-ai-api-gateway" for connection in connections
    )
    assert readiness.body["state"] == "setup-required"
    assert gateway.config_store.load().policy.policy_id == ("generic-stanford-ai-api-gateway")
    assert (
        rest.handle(
            RestRequest(
                method="PUT",
                path="/settings/models/source",
                body=json.dumps({"source_id": "unknown"}),
            )
        ).status_code
        == 422
    )
    assert (
        rest.handle(
            RestRequest(
                method="PUT",
                path="/settings/models/source",
                body=json.dumps({"source_id": "local", "extra": True}),
            )
        ).status_code
        == 422
    )


def test_rest_starts_local_model_download_and_validates_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)
    monkeypatch.setattr(
        gateway,
        "download_local_model",
        lambda model_id: {
            "model_id": model_id,
            "status": "downloading",
            "bytes_downloaded": 0,
            "bytes_total": 1,
        },
    )

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/downloads",
            body=json.dumps({"model_id": "llama-cpp-stories260k-ci"}),
        )
    )

    assert response.status_code == 202
    assert response.body["model_id"] == "llama-cpp-stories260k-ci"
    assert response.body["status"] == "downloading"
    assert (
        rest.handle(
            RestRequest(method="POST", path="/settings/models/downloads", body="{")
        ).status_code
        == 400
    )
    assert (
        rest.handle(
            RestRequest(method="POST", path="/settings/models/downloads", body="{}")
        ).status_code
        == 422
    )
    assert (
        rest.handle(
            RestRequest(
                method="POST",
                path="/settings/models/downloads",
                body=json.dumps({"model_id": "model", "token": "forbidden"}),
            )
        ).status_code
        == 422
    )


def test_rest_plans_and_starts_automatic_repository_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)
    plan = {
        "model": {"model_id": "hf-model"},
        "selection_reason": "Selected automatically",
    }
    monkeypatch.setattr(gateway, "inspect_model_repository", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        gateway,
        "download_custom_local_model",
        lambda repository, **_kwargs: {
            "model_id": f"download-{repository.replace('/', '-')}",
            "status": "downloading",
            "bytes_downloaded": 0,
            "bytes_total": 1,
        },
    )

    inspected = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/repository",
            body=json.dumps({"repository": "example/model"}),
        )
    )
    downloaded = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/downloads/custom",
            body=json.dumps({"repository": "example/model", "revision": "1" * 40}),
        )
    )

    assert inspected.status_code == 200
    assert inspected.body["selection_reason"] == "Selected automatically"
    assert downloaded.status_code == 202
    assert downloaded.body["model_id"] == "download-example-model"
    assert (
        rest.handle(
            RestRequest(
                method="POST",
                path="/settings/models/downloads/custom",
                body=json.dumps({"repository": "example/model", "runtime": "vllm"}),
            )
        ).status_code
        == 422
    )


def test_user_selected_model_plan_persists_across_gateway_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice = LocalModelChoice(
        model_id="hf-research-model-123456789abc",
        label="Research Model Q4_K_M",
        purpose="User-selected Hugging Face model.",
        runtime="llama-cpp",
        source_repository="example/research-model-gguf",
        source_revision="1" * 40,
        source_path="model-q4_k_m.gguf",
        size_bytes=7,
        minimum_free_bytes=7,
        license_posture="Source model card reports apache-2.0.",
        catalog_source="user-selected",
        artifact_sha256="a" * 64,
        minimum_resource_envelope="Estimated minimum resources",
        recommended_resource_envelope="Recommended resources",
    )

    @dataclass
    class Repository:
        calls: int = 0

        def plan(self, *_args: object, **_kwargs: object) -> LocalModelDownloadPlan:
            self.calls += 1
            return LocalModelDownloadPlan(choice, "Selected automatically")

    repository = Repository()
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
        model_repository=cast(Any, repository),
    )

    def download(artifact: ModelArtifact, *, cache_dir: Path) -> Path:
        destination = cache_dir / artifact.artifact_id / artifact.source_path
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"content")
        return destination

    monkeypatch.setattr("heartwood.gateway._gateway.download_artifact", download)

    plan = gateway.inspect_model_repository("example/research-model-gguf")
    path = gateway.download_custom_local_model_now(
        choice.source_repository,
        revision=choice.source_revision,
    )

    assert repository.calls == 1
    model = cast(dict[str, object], plan["model"])
    assert model["runtime"] == "llama-cpp"
    assert path.is_file()
    restarted = _gateway(tmp_path)
    catalog = restarted.model_artifacts()
    models = cast(list[dict[str, object]], catalog["models"])
    selected = next(model for model in models if model["model_id"] == choice.model_id)
    assert selected["source_repository"] == choice.source_repository
    assert selected["catalog_source"] == "user-selected"
    assert selected["license_posture"] == choice.license_posture
    assert selected["minimum_resource_envelope"] == choice.minimum_resource_envelope
    assert selected["recommended_resource_envelope"] == choice.recommended_resource_envelope
    assert selected["artifact_sha256"] == choice.artifact_sha256
    assert restarted.config_store.load().local_model is not None


def test_gateway_downloads_recommended_artifacts_and_snapshots_through_one_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(gateway, "_local_runtime_available", lambda _runtime: True)
    observed: list[tuple[str, str, Path]] = []

    def artifact_download(artifact: ModelArtifact, *, cache_dir: Path) -> Path:
        artifact_id = artifact.artifact_id
        observed.append(("artifact", artifact_id, cache_dir))
        path = cache_dir / artifact_id
        path.mkdir(parents=True)
        return path

    def snapshot_download(snapshot: ModelSnapshot, *, cache_dir: Path) -> Path:
        snapshot_id = snapshot.snapshot_id
        observed.append(("snapshot", snapshot_id, cache_dir))
        path = cache_dir / snapshot_id
        path.mkdir(parents=True)
        return path

    monkeypatch.setattr("heartwood.gateway._gateway.download_artifact", artifact_download)
    monkeypatch.setattr("heartwood.gateway._gateway.download_model_snapshot", snapshot_download)

    artifact = gateway.download_local_model_now("llama-cpp-stories260k-ci")
    snapshot = gateway.download_local_model_now("qwen25-7b-instruct-vllm")

    model_cache = tmp_path / ".heartwood" / "models"
    assert artifact == model_cache / "llama-cpp-stories260k-ci"
    assert snapshot == model_cache / "qwen25-7b-instruct-vllm"
    assert observed == [
        ("artifact", "llama-cpp-stories260k-ci", model_cache),
        ("snapshot", "qwen25-7b-instruct-vllm", model_cache),
    ]
    config = gateway.config_store.load()
    assert config.model_source == "local"
    assert config.local_model is not None
    assert config.local_model.artifact_id == "qwen25-7b-instruct-vllm"
    assert config.model_settings.active_profile == "local"
    assert config.model_settings.profile().model == "openai/heartwood-local-model"
    restarted = _gateway(tmp_path)
    assert restarted.model_settings()["active_profile"] == "local"
    assert restarted.project_readiness()["state"] == "compute-required"
    with pytest.raises(ModelRepositoryError, match="unknown recommended local model: missing"):
        gateway.download_local_model_now("missing")


def test_gateway_does_not_mask_unexpected_artifact_catalog_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path / "sessions")
    monkeypatch.setattr(gateway, "_local_runtime_available", lambda _runtime: True)

    def fail_lookup(_catalog: ModelArtifactCatalog, _model_id: str) -> ModelArtifact:
        raise ValueError("artifact catalog validation failed")

    monkeypatch.setattr(ModelArtifactCatalog, "artifact", fail_lookup)

    with pytest.raises(ValueError, match="artifact catalog validation failed"):
        gateway.download_local_model_now("qwen25-7b-instruct-vllm")


def test_rest_model_settings_routes_report_invalid_requests(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))

    responses = (
        rest.handle(RestRequest(method="POST", path="/settings/models/profiles", body="{")),
        rest.handle(RestRequest(method="POST", path="/settings/models/profiles", body="{}")),
        rest.handle(RestRequest(method="PUT", path="/settings/models/active", body="{")),
        rest.handle(RestRequest(method="PUT", path="/settings/models/active", body="{}")),
        rest.handle(
            RestRequest(
                method="PUT",
                path="/settings/models/active",
                body=json.dumps({"profile_id": "missing"}),
            )
        ),
        rest.handle(RestRequest(method="GET", path="/settings/models/validation")),
        rest.handle(RestRequest(method="POST", path="/settings/models/connect", body="{")),
        rest.handle(RestRequest(method="POST", path="/settings/models/connect", body="[]")),
        rest.handle(RestRequest(method="POST", path="/settings/models/connect", body="{}")),
        rest.handle(
            RestRequest(
                method="POST",
                path="/settings/models/connect",
                body=json.dumps(
                    {"preset_id": "openai", "model_name": "model", "api_key": "secret"}
                ),
            )
        ),
        rest.handle(RestRequest(method="POST", path="/settings/models/catalog", body="{")),
        rest.handle(RestRequest(method="POST", path="/settings/models/catalog", body="[]")),
        rest.handle(RestRequest(method="POST", path="/settings/models/catalog", body="{}")),
        rest.handle(
            RestRequest(
                method="POST",
                path="/settings/models/catalog",
                body=json.dumps({"connection_id": "local", "refresh": "yes"}),
            )
        ),
        rest.handle(
            RestRequest(
                method="POST",
                path="/settings/models/connect",
                body=json.dumps({"connection_id": "local", "model_id": 7}),
            )
        ),
        rest.handle(RestRequest(method="DELETE", path="/settings/models/profiles/missing")),
    )

    assert [response.status_code for response in responses] == [
        400,
        422,
        400,
        422,
        422,
        422,
        400,
        422,
        422,
        422,
        400,
        422,
        422,
        422,
        422,
        422,
    ]


def test_gateway_rejects_unknown_backend_configuration(tmp_path: Path) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="unknown",
    )

    with pytest.raises(ValueError, match="unsupported agent backend"):
        gateway.handle(SessionCommand.model_validate_json(_command(CommandKind.CHAT, prompt="hi")))


def test_rest_manages_skill_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    rest = RestGateway(gateway)
    candidate = {
        "name": "community-summary",
        "skill_id": "example.community-summary",
        "source": "candidate",
    }
    monkeypatch.setattr(gateway, "inspect_skill", lambda _source: candidate)
    monkeypatch.setattr(
        gateway,
        "install_skill",
        lambda _source, approved: (
            {"skills": [{**candidate, "source": "installed"}]} if approved else {}
        ),
    )
    monkeypatch.setattr(gateway, "remove_skill", lambda _name: {"skills": []})

    listed = rest.handle(RestRequest(method="GET", path="/settings/skills"))
    inspected = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/skills/inspect",
            body=json.dumps({"source": "/mnt/community-summary"}),
        )
    )
    installed = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/skills/install",
            body=json.dumps({"source": "/mnt/community-summary", "approved": True}),
        )
    )
    removed = rest.handle(RestRequest(method="DELETE", path="/settings/skills/community-summary"))

    assert listed.status_code == 200
    assert len(cast(list[object], listed.body["skills"])) == 3
    assert inspected.body["name"] == "community-summary"
    assert installed.status_code == 200
    assert removed.body == {"skills": []}


def test_rest_skill_routes_validate_request_shapes(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))
    responses = (
        rest.handle(RestRequest(method="POST", path="/settings/skills/inspect", body="{")),
        rest.handle(RestRequest(method="POST", path="/settings/skills/inspect", body="{}")),
        rest.handle(RestRequest(method="POST", path="/settings/skills/install", body="{")),
        rest.handle(RestRequest(method="POST", path="/settings/skills/install", body="{}")),
        rest.handle(
            RestRequest(
                method="POST",
                path="/settings/skills/install",
                body=json.dumps({"source": "/tmp/skill", "approved": "yes"}),
            )
        ),
        rest.handle(RestRequest(method="DELETE", path="/settings/skills/missing")),
    )

    assert [response.status_code for response in responses] == [400, 422, 400, 422, 422, 422]
