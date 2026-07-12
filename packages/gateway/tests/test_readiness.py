# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from heartwood.gateway import (
    ModelCatalogService,
    ModelConnection,
    ProviderModel,
    SessionGateway,
    inspect_deployment,
    persist_deployment_profile,
)


def test_readiness_is_setup_required_without_mutating_state(tmp_path: Path) -> None:
    workspace = tmp_path / "state" / "sessions"
    readiness = inspect_deployment(workspace, env={})
    assert readiness.state == "setup-required"
    assert readiness.platform_id == "generic"
    assert not (tmp_path / "state").exists()


def test_carina_readiness_reports_allocation_scratch_and_gpu(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    readiness = inspect_deployment(
        tmp_path / "state" / "sessions",
        env={
            "HEARTWOOD_PLATFORM": "carina",
            "SLURM_JOB_ID": "123",
            "LOCAL_SCRATCH_JOB": str(scratch),
            "CUDA_VISIBLE_DEVICES": "0",
        },
    )
    assert readiness.platform_id == "carina"
    assert readiness.state == "setup-required"
    assert {check.check_id: check.status for check in readiness.checks} == {
        "state-storage": "pass",
        "setup": "warning",
        "model": "warning",
        "slurm-allocation": "pass",
        "job-scratch": "pass",
        "gpu": "pass",
    }


def test_carina_without_compute_allocation_requires_recovery(tmp_path: Path) -> None:
    readiness = inspect_deployment(
        tmp_path / "state" / "sessions", env={"HEARTWOOD_PLATFORM": "carina"}
    )
    assert readiness.state == "recovery-required"


def test_carina_reports_optional_gpu_and_scratch_as_warnings(tmp_path: Path) -> None:
    readiness = inspect_deployment(
        tmp_path / "state" / "sessions",
        env={"HEARTWOOD_PLATFORM": "carina", "SLURM_JOB_ID": "123"},
    )
    checks = {check.check_id: check.status for check in readiness.checks}
    assert checks["job-scratch"] == "warning"
    assert checks["gpu"] == "warning"


def test_completed_setup_with_active_model_is_ready(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "setup.json").write_text(
        json.dumps({"schema_version": "heartwood.setup.v1"}), encoding="utf-8"
    )
    (state / "models.json").write_text(
        json.dumps(
            {
                "active_profile": "local",
                "profiles": [{"profile_id": "local", "credential_kind": "none"}],
            }
        ),
        encoding="utf-8",
    )
    (state / "policy.json").write_text(
        json.dumps(
            {
                "schema_version": "heartwood.policy-profile.v1",
                "policy_id": "test",
                "platform_id": "generic",
            }
        ),
        encoding="utf-8",
    )

    readiness = inspect_deployment(state / "sessions", env={})

    assert readiness.state == "ready"


def test_malformed_setup_and_model_files_are_not_ready(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "setup.json").write_text("not-json", encoding="utf-8")
    (state / "models.json").write_text("[]", encoding="utf-8")

    readiness = inspect_deployment(state / "sessions", env={})

    assert readiness.state == "recovery-required"


def test_ready_setup_fails_when_external_credential_is_missing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    persist_deployment_profile(state / "sessions", model_source="stanford-ai-api-gateway", env={})
    (state / "models.json").write_text(
        json.dumps(
            {
                "active_profile": "stanford-ai-api-gateway",
                "profiles": [
                    {
                        "profile_id": "stanford-ai-api-gateway",
                        "credential_kind": "environment",
                        "api_key_env": "STANFORD_AI_API_KEY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    readiness = inspect_deployment(state / "sessions", env={})

    assert readiness.state == "recovery-required"
    credential = next(check for check in readiness.checks if check.check_id == "model-credential")
    assert credential.status == "fail"
    assert "STANFORD_AI_API_KEY" in credential.summary


@pytest.mark.parametrize(
    ("profile", "expected_status"),
    [
        ({"credential_kind": "file", "api_key_file": "/missing/key"}, "fail"),
        ({"credential_kind": "managed-identity"}, "warning"),
        ({"credential_kind": "unsupported"}, "fail"),
    ],
)
def test_doctor_reports_non_environment_credential_readiness(
    tmp_path: Path,
    profile: dict[str, object],
    expected_status: str,
) -> None:
    state = tmp_path / "state"
    persist_deployment_profile(state / "sessions", model_source="local", env={})
    profile["profile_id"] = "configured"
    (state / "models.json").write_text(
        json.dumps({"active_profile": "configured", "profiles": [profile]}), encoding="utf-8"
    )

    readiness = inspect_deployment(state / "sessions", env={})

    credential = next(check for check in readiness.checks if check.check_id == "model-credential")
    assert credential.status == expected_status


def test_doctor_rejects_missing_environment_credential_reference(tmp_path: Path) -> None:
    state = tmp_path / "state"
    persist_deployment_profile(state / "sessions", model_source="local", env={})
    (state / "models.json").write_text(
        json.dumps(
            {
                "active_profile": "configured",
                "profiles": [{"profile_id": "configured", "credential_kind": "environment"}],
            }
        ),
        encoding="utf-8",
    )

    readiness = inspect_deployment(state / "sessions", env={})

    credential = next(check for check in readiness.checks if check.check_id == "model-credential")
    assert credential.status == "fail"
    assert credential.summary == "Selected model has an invalid environment credential reference"


def test_local_setup_persists_conservative_restart_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "state" / "sessions"
    setup, policy, connections = persist_deployment_profile(
        workspace, model_source="local", env={"HEARTWOOD_PLATFORM": "carina"}
    )
    assert setup.stat().st_mode & 0o777 == 0o600
    assert policy.stat().st_mode & 0o777 == 0o600
    assert connections.stat().st_mode & 0o777 == 0o600
    payload = json.loads(policy.read_text(encoding="utf-8"))
    assert payload["platform_id"] == "carina"
    assert payload["allowed_action_confirmation_modes"] == ["always-confirm"]
    assert payload["credential_allowlist"] == []


def test_stanford_setup_is_discovered_after_gateway_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "state" / "sessions"
    persist_deployment_profile(workspace, model_source="stanford-ai-api-gateway", env={})
    gateway = SessionGateway(workspace=workspace, env={"STANFORD_AI_API_KEY": "secret"})
    settings = gateway.model_settings()
    connections = settings["connections"]
    assert isinstance(connections, list)
    connection = next(
        item
        for item in connections
        if isinstance(item, dict) and item["connection_id"] == "stanford-ai-api-gateway"
    )
    assert connection["protocol"] == "openai-compatible"
    assert connection["catalog_endpoint"] == "https://aiapi-prod.stanford.edu/v1/models"
    assert connection["policy_endpoint"] == ("https://aiapi-prod.stanford.edu/v1/chat/completions")
    assert connection["credential_status"] == "available"
    assert "secret" not in json.dumps(settings)


def test_stanford_catalog_uses_exact_alias_and_external_key(tmp_path: Path) -> None:
    workspace = tmp_path / "state" / "sessions"
    persist_deployment_profile(workspace, model_source="stanford-ai-api-gateway", env={})
    observed: list[tuple[str, str | None]] = []

    def list_models(connection: ModelConnection, api_key: str | None) -> tuple[ProviderModel, ...]:
        observed.append((connection.connection_id, api_key))
        return (ProviderModel(model_id="claude-sonnet-4-6"),)

    gateway = SessionGateway(
        workspace=workspace,
        env={"STANFORD_AI_API_KEY": "secret"},
        model_catalog_service=ModelCatalogService(
            openai_lister=list_models,
            compatibility=lambda _connection, _model: (
                "available",
                "verified by test",
                None,
                True,
            ),
        ),
    )
    catalog = gateway.discover_models("stanford-ai-api-gateway", refresh=True)
    settings = gateway.connect_model("stanford-ai-api-gateway", "claude-sonnet-4-6")

    assert observed == [("stanford-ai-api-gateway", "secret")]
    catalog_models = cast(list[dict[str, object]], catalog["models"])
    profiles = cast(list[dict[str, object]], settings["profiles"])
    assert catalog_models[0]["model_id"] == "claude-sonnet-4-6"
    assert settings["active_profile"] == "stanford-ai-api-gateway"
    assert profiles[0]["model"] == "openai/claude-sonnet-4-6"
    assert "secret" not in (tmp_path / "state" / "models.json").read_text(encoding="utf-8")
