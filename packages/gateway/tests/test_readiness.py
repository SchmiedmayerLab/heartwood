# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway import (
    ModelCatalogService,
    ModelConnection,
    ModelProfile,
    ModelSettings,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    ProviderModel,
    SessionGateway,
    inspect_deployment,
    persist_deployment_profile,
)


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


def _configure_model(
    project: ProjectContext,
    *,
    source: str,
    env: dict[str, str],
) -> None:
    persist_deployment_profile(project, model_source=source, env=env)  # type: ignore[arg-type]
    store = _store(project, env)
    config = store.load()
    if source == "local":
        profile = ModelProfile(
            profile_id="local",
            model="openai/heartwood-local-model",
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
    _configure_model(project, source="local", env={})

    stopped = inspect_deployment(project, env={})
    readiness = inspect_deployment(project, env={"HEARTWOOD_LOCAL_RUNTIME_ACTIVE": "1"})

    assert stopped.state == "compute-required"
    assert readiness.state == "ready"
    artifact = next(check for check in readiness.checks if check.check_id == "local-model-artifact")
    assert artifact.status == "pass"
    assert "synthetic-model" in artifact.summary


def test_downloaded_local_model_requires_managed_runtime_before_setup(tmp_path: Path) -> None:
    project = _project(tmp_path)
    persist_deployment_profile(project, model_source="local", env={})
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
        env={"HEARTWOOD_LOCAL_RUNTIME_ACTIVE": "1"},
    )

    assert stopped.state == "compute-required"
    assert running.state == "setup-required"


def test_carina_local_project_requires_and_validates_compute(tmp_path: Path) -> None:
    project = _project(tmp_path)
    carina = {"HEARTWOOD_PLATFORM": "carina"}
    _configure_model(project, source="local", env=carina)

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
    _configure_model(project, source="stanford-ai-api-gateway", env={})

    readiness = inspect_deployment(project, env={})

    assert readiness.state == "setup-required"
    credential = next(check for check in readiness.checks if check.check_id == "model-credential")
    assert credential.status == "warning"
    assert credential.summary == "A provider credential is required for this process"


def test_configuration_for_another_detected_platform_requires_recovery(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _configure_model(project, source="local", env={})

    readiness = inspect_deployment(project, env={"HEARTWOOD_PLATFORM": "carina"})

    assert readiness.state == "recovery-required"
    configuration = next(check for check in readiness.checks if check.check_id == "configuration")
    assert configuration.status == "fail"


def test_local_setup_uses_one_private_project_configuration(tmp_path: Path) -> None:
    project = _project(tmp_path)

    path = persist_deployment_profile(
        project,
        model_source="local",
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
    persist_deployment_profile(project, model_source="stanford-ai-api-gateway", env={})

    settings = SessionGateway(
        project=project,
        env={"STANFORD_AI_API_KEY": "external-secret"},
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
    persist_deployment_profile(project, model_source="stanford-ai-api-gateway", env={})
    observed: list[tuple[str, str | None]] = []

    def list_models(connection: ModelConnection, api_key: str | None) -> tuple[ProviderModel, ...]:
        observed.append((connection.connection_id, api_key))
        return (ProviderModel(model_id="claude-sonnet-4-6"),)

    gateway = SessionGateway(
        project=project,
        env={"STANFORD_AI_API_KEY": "external-secret"},
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
