# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for gateway command handling and replayable event streams."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from heartwood.core_adapter import SessionResult
from heartwood.gateway import (
    GpuCapacity,
    GpuEnvironment,
    LocalModelChoice,
    LocalModelDownloadPlan,
    ModelArtifact,
    ModelArtifactCatalog,
    ModelCatalogService,
    ModelDownload,
    ModelRepositoryError,
    ModelSnapshot,
    ProjectContext,
    ProviderModel,
    RestGateway,
    RestRequest,
    RestResponse,
    SessionGateway,
)
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand, SessionEvent


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


def _gateway(workspace: Path, *, env: dict[str, str] | None = None) -> SessionGateway:
    workspace.mkdir(parents=True, exist_ok=True)
    return SessionGateway(
        project=ProjectContext(workspace),
        env={} if env is None else env,
        backend_id="deterministic",
    )


def test_gateway_lifecycle_does_not_load_openhands_before_agent_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared: list[dict[str, str]] = []
    monkeypatch.setattr(
        "heartwood.gateway._openhands_sdk.prepare_openhands_sdk",
        lambda env: prepared.append(env),
    )
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="auto",
    )

    gateway.start()

    assert prepared == []


def test_deterministic_gateway_does_not_load_openhands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._openhands_sdk.prepare_openhands_sdk",
        lambda: pytest.fail("deterministic gateway loaded OpenHands"),
    )
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
    )

    gateway.start()


@dataclass
class _BlockingSessionService:
    entered: Event
    release: Event
    closed: Event

    def handle(self, _command: SessionCommand) -> SessionResult:
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test session was not released")
        return SessionResult(events=())

    def replay_events(self) -> tuple[SessionEvent, ...]:
        return ()

    def close(self) -> None:
        self.closed.set()


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


def test_rest_ensures_one_shared_default_session(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path / "project")
    rest = RestGateway(gateway)

    first = rest.handle(RestRequest(method="POST", path="/sessions/default"))
    second = rest.handle(RestRequest(method="POST", path="/sessions/default"))

    assert first.status_code == 200
    assert first.body == second.body
    assert first.body["session_id"] == "session-main"
    assert first.body["title"] == "Main session"
    assert gateway.sessions() == {"sessions": [first.body]}


def test_download_completion_waits_for_an_active_session_turn(tmp_path: Path) -> None:
    entered = Event()
    release = Event()
    closed = Event()
    selection_started = Event()
    selection_finished = Event()
    service = _BlockingSessionService(entered=entered, release=release, closed=closed)
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
        service_factory=lambda _root, _session_id: cast(Any, service),
    )
    model_id = "qwen25-7b-instruct-q4_k_m"
    model_path = gateway.project.models_dir / model_id / "model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"synthetic")
    command = SessionCommand(
        command_id="command-1",
        session_id="session-1",
        kind=CommandKind.PAUSE,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload={},
    )

    def select_download() -> None:
        selection_started.set()
        gateway._select_downloaded_local_model(model_id, model_path, "llama-cpp-cpu")
        selection_finished.set()

    with ThreadPoolExecutor(max_workers=3) as executor:
        command_future = executor.submit(gateway.handle, command)
        selection_future = None
        try:
            assert entered.wait(timeout=1)
            status = executor.submit(gateway.model_artifacts).result(timeout=1)
            assert status["downloads"] == []
            selection_future = executor.submit(select_download)
            assert selection_started.wait(timeout=1)
            assert not selection_finished.wait(timeout=0.1)
            assert not closed.is_set()
        finally:
            release.set()
        assert command_future.result(timeout=2).events == ()
        assert selection_future is not None
        selection_future.result(timeout=2)

    selected = gateway.config_store.load().local_model
    assert selected is not None
    assert selected.artifact_id == model_id
    assert closed.is_set()


def test_rest_command_routes_through_session_service_and_streams_events(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    stream = gateway.websocket(session_id="session-1")
    rest = RestGateway(gateway)

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.PAUSE),
        )
    )

    assert response.status_code == 200
    assert [event["kind"] for event in _events(response)] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_PAUSED.value,
    ]
    assert [event.kind for event in stream.receive()] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_PAUSED.value,
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
    assert not (tmp_path / ".heartwood").exists()


def test_rest_command_persists_gateway_audit_log(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.PAUSE),
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
            body=_command(CommandKind.PAUSE),
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
                CommandKind.CHAT,
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
                CommandKind.CHAT,
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
            body=_command(CommandKind.PAUSE, session_id="other-session"),
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


def test_rest_exposes_shared_startup_contract_and_initializes_explicitly(tmp_path: Path) -> None:
    project = tmp_path / "analysis"
    gateway = _gateway(project)
    rest = RestGateway(gateway)

    capabilities = rest.handle(RestRequest(method="GET", path="/project/capabilities"))
    startup = rest.handle(
        RestRequest(method="GET", path="/project/startup?interface=notebook&port=9000")
    )
    invalid_interface = rest.handle(
        RestRequest(method="GET", path="/project/startup?interface=desktop")
    )
    invalid_port = rest.handle(RestRequest(method="GET", path="/project/startup?port=none"))

    assert capabilities.status_code == 200
    assert capabilities.body["platform_id"] == "generic"
    assert startup.status_code == 200
    assert startup.body["phase"] == "project-review"
    assert startup.body["interface"] == "notebook"
    assert invalid_interface.status_code == 422
    assert invalid_port.status_code == 422
    assert not (project / ".heartwood").exists()

    initialized = rest.handle(RestRequest(method="POST", path="/project/initialize"))

    assert initialized.status_code == 200
    assert initialized.body["phase"] == "connection-required"
    assert (project / ".heartwood").is_dir()


def test_rest_reports_and_forgets_credentials_without_returning_secrets(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    gateway.credential_store.save("OPENAI_API_KEY", "synthetic-secret")
    rest = RestGateway(gateway)

    available = rest.handle(RestRequest(method="GET", path="/settings/credentials"))
    forgotten = rest.handle(RestRequest(method="DELETE", path="/settings/credentials/openai"))
    unknown = rest.handle(RestRequest(method="DELETE", path="/settings/credentials/unknown"))
    credential_free = rest.handle(RestRequest(method="DELETE", path="/settings/credentials/local"))

    assert available.status_code == 200
    assert "synthetic-secret" not in json.dumps(available.body)
    assert any(
        binding["binding_id"] == "OPENAI_API_KEY" and binding["configured"] is True
        for binding in cast(list[dict[str, JsonValue]], available.body["bindings"])
    )
    assert forgotten.status_code == 200
    assert unknown.status_code == 422
    assert credential_free.status_code == 422


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        ("{", 400),
        ("{}", 422),
        (
            json.dumps(
                {
                    "path": 1,
                    "repository": "example/model",
                    "revision": "1" * 40,
                    "license": "Apache-2.0",
                }
            ),
            422,
        ),
        (
            json.dumps(
                {
                    "path": "/missing/model.gguf",
                    "repository": "example/model",
                    "revision": "1" * 40,
                    "license": "Apache-2.0",
                    "context_window": True,
                }
            ),
            422,
        ),
        (
            json.dumps(
                {
                    "path": "/missing/model.gguf",
                    "repository": "example/model",
                    "revision": "1" * 40,
                    "license": "Apache-2.0",
                }
            ),
            422,
        ),
    ],
)
def test_rest_rejects_invalid_local_model_imports(
    tmp_path: Path,
    body: str,
    expected_status: int,
) -> None:
    response = RestGateway(_gateway(tmp_path)).handle(
        RestRequest(method="POST", path="/settings/models/imports", body=body)
    )

    assert response.status_code == expected_status


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
    gateway.initialize_project()
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
        "profile_id": "custom-loopback",
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
        "description": "Compatible-service fixture",
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
            body=json.dumps({"profile_id": "custom-loopback"}),
        )
    )
    validated = rest.handle(
        RestRequest(method="GET", path="/settings/models/validation?profile_id=custom-loopback")
    )
    artifacts = rest.handle(RestRequest(method="GET", path="/settings/models/artifacts"))
    removed = rest.handle(
        RestRequest(method="DELETE", path="/settings/models/profiles/custom-loopback")
    )
    assert initial.status_code == 200
    assert initial.body["model_source"] is None
    source_options = cast(list[dict[str, JsonValue]], initial.body["source_options"])
    assert [option["source_id"] for option in source_options] == [
        "heartwood",
        "openai",
        "anthropic",
        "custom",
    ]
    connections = cast(list[dict[str, JsonValue]], initial.body["connections"])
    assert [connection["connection_id"] for connection in connections] == [
        "heartwood",
        "openai",
        "anthropic",
        "custom-api",
    ]
    assert all(
        "token" not in connection and "api_key" not in connection for connection in connections
    )
    assert saved.status_code == 200
    assert selected.body["active_profile"] == "custom-loopback"
    assert validated.status_code == 200
    assert artifacts.status_code == 200
    assert artifacts.body["schema_version"] == "heartwood.local-model-catalog.v1"
    assert artifacts.body["snapshot_schema_version"] == "heartwood.model-snapshot-catalog.v2"
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


def test_rest_reserves_the_heartwood_managed_profile(tmp_path: Path) -> None:
    rest = RestGateway(_gateway(tmp_path))
    profile = {
        "profile_id": "heartwood",
        "model": "openai/custom-model",
        "policy_endpoint": "http://127.0.0.1:8765/v1/chat/completions",
        "capability_tier": "supervised",
        "base_url": "http://127.0.0.1:8765/v1",
        "credential_kind": "none",
        "api_key_env": None,
        "api_key_file": None,
        "api_version": None,
        "aws_region_name": None,
        "aws_profile_name": None,
        "description": "Attempted replacement",
    }

    saved = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/profiles",
            body=json.dumps(profile),
        )
    )
    removed = rest.handle(RestRequest(method="DELETE", path="/settings/models/profiles/heartwood"))

    assert saved.status_code == 422
    assert "reserved by Heartwood" in str(saved.body["error"])
    assert removed.status_code == 422
    assert "managed by Heartwood" in str(removed.body["error"])


def test_gpu_environment_is_cached_and_can_be_refreshed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def inspect(platform_id: str, _env: Mapping[str, str]) -> GpuEnvironment:
        nonlocal calls
        calls += 1
        return GpuEnvironment(
            platform_id=platform_id,
            visible_devices=(),
            slurm_partitions=(),
            capacities=(),
        )

    monkeypatch.setattr("heartwood.gateway._gateway.inspect_gpu_environment", inspect)
    gateway = _gateway(tmp_path)

    first = gateway.gpu_environment()
    second = gateway.gpu_environment()
    refreshed = gateway.gpu_environment(refresh=True)

    assert first is second
    assert refreshed is not first
    assert calls == 2


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
    assert available[0]["model_id"] == "qwen25-7b-instruct-q4_k_m"
    assert available[0]["active"] is False
    assert available[0]["selected"] is False
    assert available[0]["availability_reason"] == "Recommended for this deployment"

    monkeypatch.setattr(
        "heartwood.gateway._gateway.shutil.which",
        lambda executable, **_kwargs: f"/runtime/{executable}",
    )
    gateway.env["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"
    monkeypatch.setattr(
        gateway,
        "gpu_environment",
        lambda: GpuEnvironment(
            platform_id="generic",
            visible_devices=(),
            slurm_partitions=(),
            capacities=(
                GpuCapacity(
                    label="4 visible NVIDIA L40S GPUs",
                    gpu_model="NVIDIA L40S",
                    gpu_count=4,
                    gpu_memory_bytes=48_000_000_000,
                    allocation_required=False,
                ),
            ),
        ),
    )
    fully_available = cast(list[dict[str, JsonValue]], gateway.model_artifacts()["models"])
    assert all(model["available"] for model in fully_available)
    expected_runtime = (
        "vllm" if any(model["runtime"] == "vllm" for model in fully_available) else "llama-cpp"
    )
    assert fully_available[0]["runtime"] == expected_runtime
    if fully_available[0]["runtime"] == "vllm":
        assert str(fully_available[0]["availability_reason"]).startswith(
            "Evaluation candidate; not yet a recommended model"
        )
        assert "Compatible with 4 visible NVIDIA L40S GPU(s)" in str(
            fully_available[0]["availability_reason"]
        )
    else:
        assert fully_available[0]["availability_reason"] == "Available on this deployment"

    monkeypatch.setattr(
        gateway,
        "gpu_environment",
        lambda: GpuEnvironment(
            platform_id="terra",
            visible_devices=(),
            slurm_partitions=(),
            capacities=(
                GpuCapacity(
                    label="1 visible NVIDIA T4 GPU",
                    gpu_model="NVIDIA T4",
                    gpu_count=1,
                    gpu_memory_bytes=16_000_000_000,
                    allocation_required=False,
                ),
            ),
        ),
    )
    terra_models = cast(list[dict[str, JsonValue]], gateway.model_artifacts()["models"])
    terra_standard = next(
        model
        for model in terra_models
        if model["model_id"] == "qwen25-coder-7b-instruct-awq-vllm"
    )
    assert terra_standard["qualification"] == "qualified"
    assert str(terra_standard["availability_reason"]).startswith(
        "Recommended for this deployment"
    )
    assert "Compatible with 1 visible NVIDIA T4 GPU(s)" in str(
        terra_standard["availability_reason"]
    )


def test_inaccessible_packaged_runtime_is_reported_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    real_is_file = Path.is_file

    def inaccessible_runtime(path: Path) -> bool:
        if path == Path("/opt/llama.cpp/llama-server"):
            raise PermissionError(path)
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", inaccessible_runtime)
    monkeypatch.setattr("heartwood.gateway._gateway.shutil.which", lambda *_args, **_kwargs: None)

    models = cast(list[dict[str, JsonValue]], gateway.model_artifacts()["models"])

    assert not any(model["available"] for model in models)


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
            body=json.dumps({"connection_id": "heartwood", "refresh": True}),
        )
    )
    connected = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/connect",
            body=json.dumps({"connection_id": "heartwood", "model_id": "local-coder"}),
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
    assert connected.body["active_profile"] == "heartwood"
    profiles = cast(list[dict[str, JsonValue]], connected.body["profiles"])
    assert profiles[0]["model"] == "openai/local-coder"
    assert gateway.config_store.load().model_source == "heartwood"
    restarted = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    assert restarted.model_settings()["active_profile"] == "heartwood"
    assert restarted.config_store.load().model_source == "heartwood"
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
        profile["profile_id"] == "heartwood"
        for profile in cast(list[dict[str, JsonValue]], switched.body["profiles"])
    )


def test_rest_shares_project_readiness_and_model_source_setup(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, env={"HEARTWOOD_PLATFORM": "carina"})
    rest = RestGateway(gateway)

    initial = rest.handle(RestRequest(method="GET", path="/project/readiness"))
    initial_settings = rest.handle(RestRequest(method="GET", path="/settings/models"))
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
    assert [
        option["source_id"]
        for option in cast(list[dict[str, JsonValue]], initial_settings.body["source_options"])
    ] == ["heartwood", "stanford-ai-api-gateway"]
    assert [
        connection["connection_id"]
        for connection in cast(list[dict[str, JsonValue]], initial_settings.body["connections"])
    ] == ["heartwood"]
    assert configured.status_code == 200
    assert configured.body["model_source"] == "stanford-ai-api-gateway"
    connections = cast(list[dict[str, JsonValue]], configured.body["connections"])
    assert any(
        connection["connection_id"] == "stanford-ai-api-gateway" for connection in connections
    )
    assert not {"openai", "anthropic", "custom-api"} & {
        str(connection["connection_id"]) for connection in connections
    }
    assert readiness.body["state"] == "setup-required"
    assert gateway.config_store.load().policy.policy_id == ("carina-stanford-ai-api-gateway")
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
                body=json.dumps({"source_id": "heartwood", "extra": True}),
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
        artifact_sha256=hashlib.sha256(b"content").hexdigest(),
        minimum_resource_envelope="Estimated minimum resources",
        recommended_resource_envelope="Recommended resources",
        recommended_ram_bytes=16 * 1024**3,
        recommended_disk_bytes=21,
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

    def download(
        artifact: ModelArtifact,
        *,
        cache_dir: Path,
        progress_callback: object = None,
    ) -> Path:
        del progress_callback
        destination = cache_dir / artifact.artifact_id / artifact.source_path
        destination.parent.mkdir(parents=True, exist_ok=True)
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
    assert models[0] == selected
    assert selected["active"] is False
    assert selected["selected"] is True
    assert str(selected["availability_reason"]).startswith("Selected for this project")
    assert selected["source_repository"] == choice.source_repository
    assert selected["catalog_source"] == "user-selected"

    restarted.env["HEARTWOOD_LOCAL_RUNTIME_ACTIVE"] = "1"
    restarted.env["HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID"] = choice.model_id
    active_models = cast(list[dict[str, object]], restarted.model_artifacts()["models"])
    active = next(model for model in active_models if model["model_id"] == choice.model_id)
    assert active["active"] is True
    assert selected["license_posture"] == choice.license_posture
    assert selected["minimum_resource_envelope"] == choice.minimum_resource_envelope
    assert selected["recommended_resource_envelope"] == choice.recommended_resource_envelope
    assert selected["artifact_sha256"] == choice.artifact_sha256
    assert restarted.config_store.load().local_model is not None
    downloads = cast(list[dict[str, object]], catalog["downloads"])
    assert downloads[0]["status"] == "ready"

    path.write_bytes(b"changed")
    tampered = cast(list[dict[str, object]], restarted.model_artifacts()["downloads"])
    assert tampered[0]["status"] == "error"
    assert "checksum" in str(tampered[0]["error"])
    path.write_bytes(b"content")
    monkeypatch.setattr(restarted, "_local_runtime_available", lambda _runtime: True)

    background_models: list[ModelArtifact | ModelSnapshot] = []

    def start_model(model: ModelArtifact | ModelSnapshot) -> ModelDownload:
        background_models.append(model)
        return ModelDownload(
            model_id=choice.model_id,
            status="downloading",
            bytes_downloaded=0,
            bytes_total=choice.size_bytes,
        )

    monkeypatch.setattr(restarted.local_model_manager, "start_model", start_model)
    background = restarted.download_local_model(choice.model_id)

    assert background["status"] == "downloading"
    assert len(background_models) == 1
    assert isinstance(background_models[0], ModelArtifact)

    path.unlink()
    restored = restarted.download_local_model_now(choice.model_id)

    assert restored == path
    assert restored.read_bytes() == b"content"


def test_gateway_downloads_recommended_artifacts_and_snapshots_through_one_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(gateway, "_local_runtime_available", lambda _runtime: True)
    observed: list[tuple[str, str, Path]] = []

    def artifact_download(
        artifact: ModelArtifact,
        *,
        cache_dir: Path,
        progress_callback: object = None,
    ) -> Path:
        del progress_callback
        artifact_id = artifact.artifact_id
        observed.append(("artifact", artifact_id, cache_dir))
        path = cache_dir / artifact_id
        path.mkdir(parents=True)
        return path

    def snapshot_download(
        snapshot: ModelSnapshot,
        *,
        cache_dir: Path,
        progress_callback: object = None,
    ) -> Path:
        del progress_callback
        snapshot_id = snapshot.snapshot_id
        observed.append(("snapshot", snapshot_id, cache_dir))
        path = cache_dir / snapshot_id
        path.mkdir(parents=True)
        return path

    monkeypatch.setattr("heartwood.gateway._gateway.download_artifact", artifact_download)
    monkeypatch.setattr("heartwood.gateway._gateway.download_model_snapshot", snapshot_download)

    artifact = gateway.download_local_model_now("llama-cpp-stories260k-ci")
    assert gateway.config_store.load().local_model is None
    snapshot_id = "qwen25-coder-7b-instruct-awq-vllm"
    snapshot = gateway.download_local_model_now(snapshot_id)

    model_cache = tmp_path / ".heartwood" / "models"
    assert artifact == model_cache / "llama-cpp-stories260k-ci"
    assert snapshot == model_cache / snapshot_id
    assert observed == [
        ("artifact", "llama-cpp-stories260k-ci", model_cache),
        ("snapshot", snapshot_id, model_cache),
    ]
    config = gateway.config_store.load()
    assert config.model_source == "heartwood"
    assert config.local_model is not None
    assert config.local_model.artifact_id == snapshot_id
    assert config.model_settings.active_profile == "heartwood"
    assert config.model_settings.profile().model == "openai/heartwood-managed-model"
    assert config.model_settings.profile().max_input_tokens == 28_672
    assert config.model_settings.profile().max_output_tokens == 4_096
    restarted = _gateway(tmp_path)
    assert restarted.model_settings()["active_profile"] == "heartwood"
    assert restarted.project_readiness()["state"] == "compute-required"
    restored_models = cast(list[dict[str, object]], restarted.model_artifacts()["models"])
    restored_selection = next(
        model for model in restored_models if model["model_id"] == snapshot_id
    )
    assert restored_selection["selected"] is True
    with pytest.raises(ModelRepositoryError, match="unknown Heartwood-managed model: missing"):
        gateway.download_local_model_now("missing")


def test_gateway_download_uses_the_normalized_model_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(gateway, "_local_runtime_available", lambda _runtime: True)
    snapshot_id = "qwen25-coder-7b-instruct-awq-vllm"
    destination = tmp_path / ".heartwood" / "models" / snapshot_id

    def fail_lookup(_catalog: ModelArtifactCatalog, _model_id: str) -> ModelArtifact:
        raise ValueError("artifact catalog validation failed")

    def snapshot_download(
        _snapshot: ModelSnapshot,
        *,
        cache_dir: Path,
        progress_callback: object = None,
    ) -> Path:
        del progress_callback
        assert cache_dir == tmp_path / ".heartwood" / "models"
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(ModelArtifactCatalog, "artifact", fail_lookup)
    monkeypatch.setattr("heartwood.gateway._gateway.download_model_snapshot", snapshot_download)

    assert gateway.download_local_model_now(snapshot_id) == destination


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
                body=json.dumps({"connection_id": "heartwood", "refresh": "yes"}),
            )
        ),
        rest.handle(
            RestRequest(
                method="POST",
                path="/settings/models/connect",
                body=json.dumps({"connection_id": "heartwood", "model_id": 7}),
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
