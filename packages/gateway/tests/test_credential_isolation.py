# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Credential-isolation policy and interface regression tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.gateway import (
    ActionSettings,
    ActionSettingsError,
    ModelConnection,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    RestGateway,
    RestRequest,
    SessionGateway,
    assess_credential_isolation,
)
from heartwood.notebook import NotebookSession

_SECRET = "synthetic-provider-secret"


def _hosted_profile() -> ModelProfile:
    return ModelProfile(
        profile_id="openai",
        model="openai/synthetic-model",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        capability_tier="supervised",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )


def _local_profile() -> ModelProfile:
    return ModelProfile(
        profile_id="heartwood",
        model="openai/heartwood-local-model",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        capability_tier="supervised",
        base_url="http://127.0.0.1:8765/v1",
        credential_kind="none",
    )


def _platform_profile() -> ModelProfile:
    return replace(
        _hosted_profile(),
        credential_kind="managed-identity",
        api_key_env=None,
    )


def _platform_connection() -> ModelConnection:
    return ModelConnection(
        connection_id="openai",
        label="Synthetic platform model",
        protocol="openai",
        model_prefix="openai/",
        source="platform",
        credential_kind="managed-identity",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        catalog_endpoint=None,
    )


def _configured_project(
    root: Path,
    *,
    profile: ModelProfile,
    confirmation_mode: str = "always-confirm",
) -> ProjectContext:
    project = ProjectContext(root)
    project.initialize()
    platform = GenericPlatformAdapter()
    settings = ModelSettings(profiles=(profile,), active_profile=profile.profile_id)
    config = ProjectConfig(
        platform_id="generic",
        model_source="heartwood" if profile.credential_kind == "none" else profile.profile_id,
        action_settings=ActionSettings().selecting(confirmation_mode),
        model_settings=settings,
        policy=platform.default_policy_profile(),
    )
    ProjectConfigStore(project, config).save(config)
    return project


def test_isolation_assessment_distinguishes_scrubbing_from_platform_isolation() -> None:
    capabilities = GenericPlatformAdapter().capabilities()

    unconfigured = assess_credential_isolation(
        None,
        capabilities,
        model_source=None,
    )
    local = assess_credential_isolation(
        _local_profile(),
        capabilities,
        model_source="heartwood",
    )
    hosted = assess_credential_isolation(
        _hosted_profile(),
        capabilities,
        model_source="openai",
    )
    isolated = assess_credential_isolation(
        _platform_profile(),
        replace(
            capabilities,
            platform_isolated_model_sources=("openai",),
            validation_level="ci-and-live-synthetic",
        ),
        model_source="openai",
        model_connections=(_platform_connection(),),
    )
    same_platform_unisolated = assess_credential_isolation(
        _hosted_profile(),
        replace(
            capabilities,
            platform_isolated_model_sources=("stanford-ai-api-gateway",),
            validation_level="ci-and-live-synthetic",
        ),
        model_source="openai",
    )

    assert unconfigured.status == "not-configured"
    assert local.status == "not-required"
    assert local.unattended_actions_allowed is True
    assert hosted.status == "review-required"
    assert hosted.boundary == "application-scrubbed"
    assert hosted.unattended_actions_allowed is False
    assert isolated.status == "qualified"
    assert isolated.boundary == "platform-isolated"
    assert isolated.unattended_actions_allowed is True
    assert same_platform_unisolated.status == "review-required"


def test_platform_isolation_requires_the_exact_managed_identity_profile() -> None:
    capabilities = replace(
        GenericPlatformAdapter().capabilities(),
        platform_isolated_model_sources=("openai",),
        validation_level="ci-and-live-synthetic",
    )

    assert (
        assess_credential_isolation(
            _hosted_profile(),
            capabilities,
            model_source="openai",
            model_connections=(_platform_connection(),),
        ).status
        == "review-required"
    )
    assert (
        assess_credential_isolation(
            replace(_platform_profile(), profile_id="other"),
            capabilities,
            model_source="openai",
            model_connections=(_platform_connection(),),
        ).status
        == "review-required"
    )
    assert (
        assess_credential_isolation(
            replace(
                _platform_profile(),
                policy_endpoint="https://api.openai.com/v1/responses",
            ),
            capabilities,
            model_source="openai",
            model_connections=(_platform_connection(),),
        ).status
        == "review-required"
    )


def test_secret_backed_model_disables_low_risk_automation_across_interfaces(
    tmp_path: Path,
) -> None:
    project = _configured_project(tmp_path, profile=_hosted_profile())
    gateway = SessionGateway(
        project=project,
        env={"OPENAI_API_KEY": _SECRET},
    )
    notebook = NotebookSession(project=project, gateway=gateway)
    try:
        model_settings = gateway.model_settings()
        action_settings = gateway.action_settings()
        readiness = gateway.deployment_readiness()
        validation = notebook.validate_model_profile("openai")

        isolation = model_settings["credential_isolation"]
        assert isolation["status"] == "review-required"
        assert isolation["boundary"] == "application-scrubbed"
        low_risk = next(
            mode for mode in action_settings["modes"] if mode["mode"] == "confirm-risky"
        )
        assert low_risk["allowed"] is False
        assert "not isolated" in str(low_risk["unavailable_reason"])
        isolation_check = next(
            check for check in readiness.checks if check.check_id == "credential-isolation"
        )
        assert readiness.state == "ready"
        assert isolation_check.status == "warning"
        assert validation["credential_isolation"] == isolation

        with pytest.raises(ActionSettingsError, match="not isolated"):
            gateway.select_action_confirmation_mode("confirm-risky")

        exposed = json.dumps(
            {
                "models": model_settings,
                "actions": action_settings,
                "readiness": readiness.safe_dict(),
                "validation": validation,
            }
        )
        assert _SECRET not in exposed
        assert _SECRET not in project.config_path.read_text(encoding="utf-8")
    finally:
        gateway.stop()


def test_unsafe_persisted_combination_fails_readiness_and_agent_start(
    tmp_path: Path,
) -> None:
    project = _configured_project(
        tmp_path,
        profile=_hosted_profile(),
        confirmation_mode="confirm-risky",
    )
    gateway = SessionGateway(
        project=project,
        env={"OPENAI_API_KEY": _SECRET},
    )
    try:
        readiness = gateway.deployment_readiness()
        isolation = next(
            check for check in readiness.checks if check.check_id == "credential-isolation"
        )

        assert readiness.state == "recovery-required"
        assert isolation.status == "fail"
        with pytest.raises(ActionSettingsError, match="not isolated"):
            gateway._service_configuration()
    finally:
        gateway.stop()


def test_credential_free_model_allows_low_risk_automation(tmp_path: Path) -> None:
    project = _configured_project(tmp_path, profile=_local_profile())
    gateway = SessionGateway(project=project, env={})
    try:
        selected = gateway.select_action_confirmation_mode("confirm-risky")
        readiness = gateway.deployment_readiness()
        isolation = next(
            check for check in readiness.checks if check.check_id == "credential-isolation"
        )

        assert selected["confirmation_mode"] == "confirm-risky"
        assert isolation.status == "pass"
        assert gateway.model_settings()["credential_isolation"]["status"] == "not-required"
    finally:
        gateway.stop()


def test_model_selection_rejects_secret_route_while_automation_is_active(
    tmp_path: Path,
) -> None:
    project = _configured_project(
        tmp_path,
        profile=_local_profile(),
        confirmation_mode="confirm-risky",
    )
    gateway = SessionGateway(project=project, env={"OPENAI_API_KEY": _SECRET})
    settings = ModelSettings(
        profiles=(_local_profile(), _hosted_profile()),
        active_profile="openai",
    )
    try:
        with pytest.raises(ModelSettingsError, match="not isolated"):
            gateway._save_model_selection("openai", settings)
        assert gateway.model_settings()["active_profile"] == "heartwood"
    finally:
        gateway.stop()


def test_secret_material_stays_out_of_shared_interfaces_state_and_restart(
    tmp_path: Path,
) -> None:
    project = _configured_project(tmp_path, profile=_hosted_profile())
    gateway = SessionGateway(
        project=project,
        env={"OPENAI_API_KEY": _SECRET},
        backend_id="deterministic",
    )
    notebook = NotebookSession(project=project, gateway=gateway)
    browser = RestGateway(gateway)
    try:
        notebook.chat("Create one synthetic result.")
        notebook_export = notebook.audit_export()
        browser_surfaces = [
            browser.handle(RestRequest(method="GET", path="/settings/models")).body,
            browser.handle(RestRequest(method="GET", path="/settings/actions")).body,
            browser.handle(RestRequest(method="GET", path="/project/readiness")).body,
            browser.handle(RestRequest(method="GET", path="/sessions/session-main/events")).body,
            browser.handle(
                RestRequest(method="GET", path="/sessions/session-main/audit-export")
            ).body,
        ]
        shared_surfaces = {
            "browser": browser_surfaces,
            "notebook": {
                "models": notebook.model_settings(),
                "validation": notebook.validate_model_profile("openai"),
                "readiness": notebook.project_readiness(),
                "replay": notebook.replay().projection.safe_dict(),
                "audit": notebook_export,
            },
            "gateway": {
                "models": gateway.model_settings(),
                "actions": gateway.action_settings(),
                "events": [
                    event.model_dump(mode="json")
                    for event in gateway.replay_events(session_id="session-main")
                ],
                "audit": gateway.audit_export("session-main"),
            },
        }

        assert _SECRET not in json.dumps(shared_surfaces)
        for path in project.state_root.rglob("*"):
            if path.is_file():
                assert _SECRET.encode() not in path.read_bytes(), path
    finally:
        gateway.stop()

    restored = SessionGateway(
        project=project,
        env={"OPENAI_API_KEY": _SECRET},
        backend_id="deterministic",
    )
    try:
        restored_surfaces = {
            "settings": restored.model_settings(),
            "projection": restored.persisted_session_projection(
                session_id="session-main"
            ).safe_dict(),
            "audit": restored.audit_export("session-main"),
        }
        assert _SECRET not in json.dumps(restored_surfaces)
        assert restored.model_settings()["credential_isolation"]["status"] == "review-required"
    finally:
        restored.stop()
