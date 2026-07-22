# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway import (
    ModelCatalogService,
    ModelConnection,
    ModelProfile,
    ModelSettings,
    ProjectConfig,
    ProjectConfigError,
    ProjectConfigStore,
    ProjectContext,
    ProviderModel,
    SessionGateway,
    inspect_deployment,
    model_source_options,
    persist_deployment_profile,
)
from heartwood.gateway._project_config import LocalModelSelection
from heartwood.gateway._readiness import managed_local_runtime_active


def _project(tmp_path: Path) -> ProjectContext:
    return ProjectContext(tmp_path)


def _store(project: ProjectContext, env: dict[str, str]) -> ProjectConfigStore:
    adapter = select_platform_adapter(env)
    return ProjectConfigStore(
        project,
        ProjectConfig(
            platform_id=adapter.adapter_id,
            policy=adapter.default_policy_profile(),
        ),
    )


def test_managed_runtime_activity_uses_exact_launch_state_or_loopback_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = LocalModelSelection(
        artifact_id="synthetic-artifact",
        path=".heartwood/models/synthetic-artifact",
        model_id="heartwood-managed-model",
    )

    assert managed_local_runtime_active(
        selection,
        {
            "HEARTWOOD_LOCAL_RUNTIME_ACTIVE": "1",
            "HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID": "synthetic-artifact",
        },
    )

    class Opener:
        def open(self, _url: str, *, timeout: float) -> BytesIO:
            assert timeout == 0.5
            return BytesIO(b'{"data":[{"id":"heartwood-managed-model"}]}')

    monkeypatch.setattr(
        "heartwood.gateway._readiness.urllib.request.build_opener",
        lambda *_args: Opener(),
    )

    assert managed_local_runtime_active(selection, {})


def test_readiness_reports_agent_dependency_failure_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_env: object) -> object:
        raise RuntimeError("synthetic import failure")

    monkeypatch.setattr("heartwood.gateway._readiness.prepare_openhands_sdk", fail)

    readiness = inspect_deployment(ProjectContext(tmp_path), env={})
    check = next(item for item in readiness.checks if item.check_id == "agent-runtime")

    assert readiness.state == "recovery-required"
    assert check.status == "fail"
    assert check.safe_dict()["code"] == "HW-AGENT-001"


def _configure_model(
    project: ProjectContext,
    *,
    source: str,
    env: dict[str, str],
) -> None:
    persist_deployment_profile(project, model_source=source, env=env)  # type: ignore[arg-type]
    store = _store(project, env)
    config = store.load()
    if source == "heartwood":
        profile = ModelProfile(
            profile_id="heartwood",
            model="openai/heartwood-managed-model",
            policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
            base_url="http://127.0.0.1:8765/v1",
            credential_kind="none",
        )
        model_path = project.models_dir / "synthetic-model"
        model_path.mkdir()
        store.select_local_model(
            artifact_id="synthetic-model",
            path=model_path,
            runtime="vllm",
            minimum_gpu_count=1,
            minimum_gpu_memory_bytes=1,
            tool_call_parser="hermes",
        )
        config = store.load()
    else:
        profile = ModelProfile(
            profile_id="stanford-ai-api-gateway",
            model="openai/claude-sonnet-4-6",
            policy_endpoint="https://aiapi-prod.stanford.edu/v1/chat/completions",
            base_url="https://aiapi-prod.stanford.edu/v1",
            credential_kind="environment",
            api_key_env="STANFORD_AI_API_KEY",
        )
    store.save(
        replace(
            config,
            model_settings=ModelSettings(
                active_profile=profile.profile_id,
                profiles=(profile,),
            ),
        )
    )


def test_generic_model_sources_use_platform_neutral_public_language() -> None:
    options = {option.source_id: option for option in model_source_options({})}

    assert options["heartwood"].label == "Run with Heartwood"
    assert "this environment" in options["heartwood"].description
    assert "computer" not in options["heartwood"].label.lower()


def test_readiness_is_read_only_before_setup(tmp_path: Path) -> None:
    project = _project(tmp_path)

    readiness = inspect_deployment(project, env={})

    assert readiness.state == "setup-required"
    assert readiness.platform_id == "generic"
    assert readiness.project_root == str(tmp_path)
    assert not project.state_root.exists()


def test_initialized_project_without_configuration_requires_setup(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.initialize()

    readiness = inspect_deployment(project, env={})

    assert readiness.state == "setup-required"
    assert next(
        check for check in readiness.checks if check.check_id == "project-state"
    ).status == ("pass")


def test_malformed_project_state_and_configuration_require_recovery(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.state_root.mkdir()
    (project.state_root / "legacy.json").write_text("{}", encoding="utf-8")

    readiness = inspect_deployment(project, env={})

    assert readiness.state == "recovery-required"
    assert any("incompatible .heartwood layout" in check.summary for check in readiness.checks)

    (project.state_root / "legacy.json").unlink()
    project.initialize()
    project.config_path.write_text("{", encoding="utf-8")
    readiness = inspect_deployment(project, env={})
    assert readiness.state == "recovery-required"
    assert any(
        "unable to load .heartwood/config.toml" in check.summary for check in readiness.checks
    )


def test_ready_local_project_reports_selected_artifact(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _configure_model(project, source="heartwood", env={})

    stopped = inspect_deployment(project, env={})
    mismatch = inspect_deployment(
        project,
        env={
            "HEARTWOOD_LOCAL_RUNTIME_ACTIVE": "1",
            "HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID": "other-model",
        },
    )
    readiness = inspect_deployment(
        project,
        env={
            "HEARTWOOD_LOCAL_RUNTIME_ACTIVE": "1",
            "HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID": "synthetic-model",
        },
    )

    assert stopped.state == "compute-required"
    assert mismatch.state == "compute-required"
    assert readiness.state == "ready"
    artifact = next(check for check in readiness.checks if check.check_id == "local-model-artifact")
    assert artifact.status == "pass"
    assert "synthetic-model" in artifact.summary


def test_terra_requires_a_dedicated_project_on_persistent_storage(tmp_path: Path) -> None:
    persistent_root = tmp_path / "jupyter"
    project_root = persistent_root / "synthetic-analysis"
    persistent_root.mkdir()
    project_root.mkdir()
    env = {
        "HEARTWOOD_PLATFORM": "terra",
        "HEARTWOOD_PLATFORM_HOME": str(persistent_root),
        "HEARTWOOD_GPU_RUNTIME": "none",
    }

    ready_boundary = inspect_deployment(ProjectContext(project_root), env=env)
    broad_boundary = inspect_deployment(ProjectContext(persistent_root), env=env)
    outside_root = tmp_path / "ephemeral"
    outside_root.mkdir()
    ephemeral_boundary = inspect_deployment(ProjectContext(outside_root), env=env)

    storage = next(
        check for check in ready_boundary.checks if check.check_id == "terra-project-storage"
    )
    runtime = next(check for check in ready_boundary.checks if check.check_id == "terra-gpu")
    assert ready_boundary.state == "setup-required"
    assert storage.status == "pass"
    assert (
        runtime.summary
        == "Portable Terra runtime selected; Heartwood-managed models use CPU inference"
    )
    assert broad_boundary.state == "recovery-required"
    assert ephemeral_boundary.state == "recovery-required"

    misleading_home = tmp_path / "ephemeral-home"
    misleading_project = misleading_home / "synthetic-analysis"
    misleading_project.mkdir(parents=True)
    unknown_mount = inspect_deployment(
        ProjectContext(misleading_project),
        env={"HEARTWOOD_PLATFORM": "terra", "HOME": str(misleading_home)},
    )
    assert unknown_mount.state == "recovery-required"

    whitespace_home = inspect_deployment(
        ProjectContext(misleading_project),
        env={"HEARTWOOD_PLATFORM": "terra", "HEARTWOOD_PLATFORM_HOME": "   "},
    )
    whitespace_storage = next(
        check for check in whitespace_home.checks if check.check_id == "terra-project-storage"
    )
    assert whitespace_home.state == "recovery-required"
    assert "/home/jupyter" in whitespace_storage.summary


def test_terra_gpu_image_reports_attachment_readiness(tmp_path: Path) -> None:
    persistent_root = tmp_path / "jupyter"
    project_root = persistent_root / "synthetic-analysis"
    project_root.mkdir(parents=True)
    base_env = {
        "HEARTWOOD_PLATFORM": "terra",
        "HEARTWOOD_PLATFORM_HOME": str(persistent_root),
        "HEARTWOOD_GPU_RUNTIME": "vllm",
    }

    missing = inspect_deployment(ProjectContext(project_root), env=base_env)
    attached = inspect_deployment(
        ProjectContext(project_root),
        env={**base_env, "CUDA_VISIBLE_DEVICES": "0"},
    )

    missing_gpu = next(check for check in missing.checks if check.check_id == "terra-gpu")
    attached_gpu = next(check for check in attached.checks if check.check_id == "terra-gpu")
    assert missing_gpu.status == "warning"
    assert attached_gpu.status == "pass"


@pytest.mark.parametrize(
    ("env", "model_source"),
    [
        ({"HEARTWOOD_PLATFORM": "carina"}, "openai"),
        ({}, "stanford-ai-api-gateway"),
    ],
)
def test_persist_deployment_profile_rejects_platform_unsupported_model_sources(
    tmp_path: Path,
    env: dict[str, str],
    model_source: str,
) -> None:
    project = ProjectContext(tmp_path)

    with pytest.raises(ProjectConfigError, match="does not provide"):
        persist_deployment_profile(
            project,
            model_source=model_source,  # type: ignore[arg-type]
            env=env,
        )

    assert not project.config_path.exists()


def test_terra_baseline_persists_builtin_hosted_provider_routes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    env = {
        "HEARTWOOD_PLATFORM": "terra",
        "HEARTWOOD_PLATFORM_HOME": str(tmp_path.parent),
    }

    gateway = SessionGateway(
        project=project,
        env=env,
        backend_id="deterministic",
        model_catalog_service=ModelCatalogService(
            openai_lister=lambda _connection, _token: (ProviderModel("gpt-synthetic"),),
            compatibility=lambda _connection, _model: (
                "available",
                "verified",
                32_768,
                True,
            ),
        ),
    )
    gateway.configure_model_source("openai")
    catalog = gateway.discover_models("openai", token="transient-secret", refresh=True)
    gateway.connect_model("openai", "gpt-synthetic")
    validation = gateway.validate_model_profile()
    policy = gateway.config_store.load().policy
    persisted = project.config_path.read_text(encoding="utf-8")

    assert policy.policy_id == "terra-default"
    assert "https://api.openai.com/v1/chat/completions" in policy.allowed_model_endpoints
    assert "https://api.anthropic.com/v1/models" in policy.allowed_model_catalog_endpoints
    assert policy.credential_allowlist == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    assert catalog["models"]
    decision = validation["policy_decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "allow"
    assert "transient-secret" not in persisted


def test_downloaded_local_model_requires_managed_runtime_before_setup(tmp_path: Path) -> None:
    project = _project(tmp_path)
    persist_deployment_profile(project, model_source="heartwood", env={})
    store = _store(project, {})
    model_path = project.models_dir / "synthetic-model"
    model_path.mkdir()
    store.select_local_model(
        artifact_id="synthetic-model",
        path=model_path,
        runtime="llama-cpp",
    )

    stopped = inspect_deployment(project, env={})
    running = inspect_deployment(
        project,
        env={
            "HEARTWOOD_LOCAL_RUNTIME_ACTIVE": "1",
            "HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID": "synthetic-model",
        },
    )

    assert stopped.state == "compute-required"
    assert running.state == "setup-required"


def test_carina_local_project_requires_and_validates_compute(tmp_path: Path) -> None:
    project = _project(tmp_path)
    carina = {"HEARTWOOD_PLATFORM": "carina"}
    _configure_model(project, source="heartwood", env=carina)

    login_readiness = inspect_deployment(project, env=carina)
    missing_runtime = inspect_deployment(
        project,
        env={**carina, "SLURM_JOB_ID": "123"},
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    allocated = inspect_deployment(
        project,
        env={
            **carina,
            "SLURM_JOB_ID": "123",
            "LOCAL_SCRATCH_JOB": str(scratch),
            "CUDA_VISIBLE_DEVICES": "0",
            "HEARTWOOD_LOCAL_RUNTIME_ACTIVE": "1",
            "HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID": "synthetic-model",
        },
    )

    assert login_readiness.state == "compute-required"
    assert missing_runtime.state == "recovery-required"
    assert allocated.state == "ready"
    assert {check.check_id for check in allocated.checks} >= {
        "slurm-allocation",
        "job-scratch",
        "gpu",
    }


def test_carina_managed_route_does_not_require_compute(tmp_path: Path) -> None:
    project = _project(tmp_path)
    env = {
        "HEARTWOOD_PLATFORM": "carina",
        "STANFORD_AI_API_KEY": "external-secret",
    }
    _configure_model(project, source="stanford-ai-api-gateway", env=env)

    readiness = inspect_deployment(project, env=env)

    assert readiness.state == "ready"
    allocation = next(check for check in readiness.checks if check.check_id == "slurm-allocation")
    assert allocation.status == "pass"
    assert "does not require" in allocation.summary


def test_missing_external_credential_requires_session_setup_without_naming_secret_value(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    env = {"HEARTWOOD_PLATFORM": "carina"}
    _configure_model(project, source="stanford-ai-api-gateway", env=env)

    readiness = inspect_deployment(project, env=env)

    assert readiness.state == "setup-required"
    credential = next(check for check in readiness.checks if check.check_id == "model-credential")
    assert credential.status == "warning"
    assert credential.summary == "A provider credential is required for this process"


def test_configuration_for_another_detected_platform_requires_recovery(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _configure_model(project, source="heartwood", env={})

    readiness = inspect_deployment(project, env={"HEARTWOOD_PLATFORM": "carina"})

    assert readiness.state == "recovery-required"
    configuration = next(check for check in readiness.checks if check.check_id == "configuration")
    assert configuration.status == "fail"


def test_local_setup_uses_one_private_project_configuration(tmp_path: Path) -> None:
    project = _project(tmp_path)

    path = persist_deployment_profile(
        project,
        model_source="heartwood",
        env={"HEARTWOOD_PLATFORM": "carina"},
    )
    gateway = SessionGateway(
        project=project,
        env={"HEARTWOOD_PLATFORM": "carina"},
    )

    assert path == project.config_path
    assert path.stat().st_mode & 0o777 == 0o600
    assert not any(
        (project.state_root / name).exists()
        for name in (
            "setup.json",
            "policy.json",
            "model-connections.json",
            "models.json",
            "actions.json",
        )
    )
    assert gateway.select_action_confirmation_mode("confirm-risky")["confirmation_mode"] == (
        "confirm-risky"
    )


def test_stanford_setup_is_available_after_gateway_restart(tmp_path: Path) -> None:
    project = _project(tmp_path)
    platform_env = {"HEARTWOOD_PLATFORM": "carina"}
    persist_deployment_profile(
        project,
        model_source="stanford-ai-api-gateway",
        env=platform_env,
    )

    settings = SessionGateway(
        project=project,
        env={**platform_env, "STANFORD_AI_API_KEY": "external-secret"},
    ).model_settings()
    connections = settings["connections"]
    assert isinstance(connections, list)
    connection = next(
        item
        for item in connections
        if isinstance(item, dict) and item["connection_id"] == "stanford-ai-api-gateway"
    )

    assert connection["catalog_endpoint"] == "https://aiapi-prod.stanford.edu/v1/models"
    assert connection["policy_endpoint"] == "https://aiapi-prod.stanford.edu/v1/chat/completions"
    assert connection["credential_status"] == "available"
    assert "external-secret" not in project.config_path.read_text(encoding="utf-8")
    assert "external-secret" not in json.dumps(settings)


def test_stanford_catalog_uses_external_key_and_exact_connection(tmp_path: Path) -> None:
    project = _project(tmp_path)
    platform_env = {"HEARTWOOD_PLATFORM": "carina"}
    persist_deployment_profile(
        project,
        model_source="stanford-ai-api-gateway",
        env=platform_env,
    )
    observed: list[tuple[str, str | None]] = []

    def list_models(connection: ModelConnection, api_key: str | None) -> tuple[ProviderModel, ...]:
        observed.append((connection.connection_id, api_key))
        return (ProviderModel(model_id="claude-sonnet-4-6"),)

    gateway = SessionGateway(
        project=project,
        env={**platform_env, "STANFORD_AI_API_KEY": "external-secret"},
        model_catalog_service=ModelCatalogService(
            openai_lister=list_models,
            compatibility=lambda _connection, _model: (
                "available",
                "verified",
                None,
                True,
            ),
        ),
    )

    catalog = gateway.discover_models("stanford-ai-api-gateway", refresh=True)

    assert observed == [("stanford-ai-api-gateway", "external-secret")]
    assert catalog["models"] == [
        {
            "availability": "available",
            "context_window": None,
            "display_name": "claude-sonnet-4-6",
            "execution_model": "openai/claude-sonnet-4-6",
            "model_id": "claude-sonnet-4-6",
            "reason": "verified",
            "supports_tools": True,
        }
    ]
