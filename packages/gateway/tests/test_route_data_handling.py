# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Declared route data handling across setup, selection, session history and audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from heartwood.core_adapter import FileSessionStore
from heartwood.gateway import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalogService,
    ModelConnection,
    ModelSettingsError,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    ProviderModel,
    SessionGateway,
)
from heartwood.gateway._readiness import persist_deployment_profile
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind, SessionCommand

_OPENAI_TOKEN = "synthetic-openai-token-value"
_ANTHROPIC_TOKEN = "synthetic-anthropic-token-value"


def test_every_built_in_route_declares_its_data_handling(tmp_path: Path) -> None:
    for connection in BUILT_IN_MODEL_CONNECTIONS:
        if connection.connection_id == "custom-api":
            # A researcher-entered service cannot declare authoritative handling.
            assert connection.data_handling is None
            continue
        assert connection.data_handling is not None, connection.connection_id
    declared = {
        connection.connection_id: connection.data_handling
        for connection in BUILT_IN_MODEL_CONNECTIONS
        if connection.data_handling is not None
    }
    assert declared["heartwood"].destination == "same-compute"
    assert {declared[name].destination for name in ("openai", "anthropic")} == {"provider"}

    project = _project(tmp_path)
    persist_deployment_profile(project, model_source="stanford-ai-api-gateway", env={})
    stanford = next(
        connection
        for connection in ProjectConfigStore(
            project, ProjectConfig(platform_id="generic", policy=_policy())
        )
        .load()
        .additional_connections
        if connection.connection_id == "stanford-ai-api-gateway"
    )
    assert stanford.data_handling is not None
    assert stanford.data_handling.destination == "institution-managed"
    assert stanford.data_handling.retention_authority == "institution"


def test_setup_responses_present_the_declared_handling(tmp_path: Path) -> None:
    gateway = SessionGateway(project=_project(tmp_path), env={}, backend_id="deterministic")
    try:
        settings = gateway.model_settings()
        connections = {
            str(item["connection_id"]): item
            for item in cast(list[dict[str, object]], settings["connections"])
        }
        assert connections["openai"]["data_handling"] == {
            "destination": "provider",
            "retention_authority": "provider",
            "route": "OpenAI API",
        }
        assert "declaration, not verified" in str(connections["openai"]["data_handling_summary"])
        assert connections["custom-api"]["data_handling"] is None
        assert connections["custom-api"]["data_handling_summary"] == (
            "Data handling is not declared for this route."
        )
        assert settings["data_handling"] is None
    finally:
        gateway.stop()


def test_destination_policy_refuses_to_select_an_undeclared_compatible_service(
    tmp_path: Path,
) -> None:
    ceiling = _policy(allowed_data_destinations=("provider",))
    gateway = _gateway(tmp_path, deployment_policy=ceiling)
    try:
        with pytest.raises(ModelSettingsError, match="requires declared data handling"):
            gateway.connect_model(
                "custom-api",
                "custom-coder",
                token="synthetic-custom-token",
                base_url="https://custom.example/v1",
                manual=True,
            )
        assert gateway.model_settings()["active_profile"] is None

        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        selected = gateway.connect_model("openai", "provider-model")
        assert selected["active_profile"] == "openai"
        assert cast(dict[str, object], selected["data_handling"])["destination"] == "provider"
        validation = gateway.validate_model_profile()
        assert cast(dict[str, object], validation["policy_decision"])["decision"] == "allow"
        assert validation["data_handling"] == selected["data_handling"]
    finally:
        gateway.stop()


def test_destination_policy_refuses_a_declared_route_outside_the_boundary(
    tmp_path: Path,
) -> None:
    gateway = _gateway(
        tmp_path, deployment_policy=_policy(allowed_data_destinations=("same-compute",))
    )
    try:
        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        with pytest.raises(ModelSettingsError, match="does not permit the provider"):
            gateway.connect_model("openai", "provider-model")
        assert gateway.model_settings()["active_profile"] is None
    finally:
        gateway.stop()


def test_route_change_records_the_new_descriptor_before_the_next_model_call(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path)
    try:
        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        gateway.connect_model("openai", "provider-model")
        gateway.handle(_command(CommandKind.CHAT, "first", prompt="Inspect the cohort"))
        gateway.discover_models("anthropic", token=_ANTHROPIC_TOKEN)
        gateway.connect_model("anthropic", "claude-synthetic")
        gateway.handle(_command(CommandKind.CHAT, "second", prompt="Continue the analysis"))
        gateway.handle(_command(CommandKind.AUDIT_EXPORT, "export"))

        replayed = FileSessionStore(gateway.sessions_root, "session-1").replay_events()
        routes = [
            cast(dict[str, object], event.payload["model_profile"])["data_handling"]
            for event in replayed
            if event.kind == EventKind.MODEL_CALL_DECISION_RECORDED
        ]
        assert [cast(dict[str, object], route)["route"] for route in routes] == [
            "OpenAI API",
            "Anthropic API",
        ]
        second_message = next(
            event.sequence
            for event in replayed
            if event.kind == EventKind.USER_MESSAGE_RECORDED
            and event.payload.get("command_id") == "second"
        )
        second_decision = next(
            event
            for event in replayed
            if event.kind == EventKind.MODEL_CALL_DECISION_RECORDED
            and event.sequence > second_message
        )
        assert all(
            event.kind != EventKind.AGENT_MESSAGE_EMITTED
            for event in replayed
            if second_message < event.sequence < second_decision.sequence
        )

        exported = gateway.audit_export("session-1")["content"]
        audited = [
            json.loads(line)["payload"]["model_profile"]["data_handling"]["route"]
            for line in exported.splitlines()
            if json.loads(line)["event_type"] == EventKind.MODEL_CALL_DECISION_RECORDED.value
        ]
        assert audited == ["OpenAI API", "Anthropic API"]
        for secret in (_OPENAI_TOKEN, _ANTHROPIC_TOKEN, "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            assert secret not in exported
    finally:
        gateway.stop()


def _gateway(tmp_path: Path, *, deployment_policy: PolicyProfile | None = None) -> SessionGateway:
    project = _project(tmp_path)
    if deployment_policy is None:
        config = ProjectConfig(platform_id="generic", policy=_policy())
        ProjectConfigStore(project, config).save(config)

    def models(connection: ModelConnection, _api_key: str | None) -> tuple[ProviderModel, ...]:
        if connection.connection_id == "anthropic":
            return (ProviderModel("claude-synthetic"),)
        return (ProviderModel("provider-model"),)

    return SessionGateway(
        project=project,
        env={},
        backend_id="deterministic",
        deployment_policy=deployment_policy,
        model_catalog_service=ModelCatalogService(
            openai_lister=models,
            anthropic_lister=models,
            compatibility=lambda _connection, _model: ("available", "verified", None, True),
        ),
    )


def _policy(**update: object) -> PolicyProfile:
    return PolicyProfile.model_validate(
        {
            "policy_id": "route-handling-test",
            "platform_id": "generic",
            "allowed_model_endpoints": [
                "https://api.openai.com/v1/chat/completions",
                "https://api.anthropic.com/v1/messages",
                "https://custom.example/v1/chat/completions",
            ],
            "allowed_model_catalog_endpoints": [
                "https://api.openai.com/v1/models",
                "https://api.anthropic.com/v1/models",
                "https://custom.example/v1/models",
            ],
            "allowed_capability_tiers": ["supervised", "experimental"],
            "allowed_action_review_modes": ["review-all"],
            "credential_allowlist": [
                "ANTHROPIC_API_KEY",
                "HEARTWOOD_CUSTOM_MODEL_API_KEY",
                "OPENAI_API_KEY",
            ],
            **update,
        }
    )


def _project(root: Path) -> ProjectContext:
    root.mkdir(parents=True, exist_ok=True)
    return ProjectContext(root)


def _command(kind: CommandKind, command_id: str, **payload: str) -> SessionCommand:
    return SessionCommand(
        command_id=command_id,
        session_id="session-1",
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=dict(payload),
    )


def test_model_source_selection_keeps_a_data_destination_restriction(tmp_path: Path) -> None:
    project = _project(tmp_path)
    restricted = _policy(allowed_data_destinations=("institution-managed",))
    config = ProjectConfig(platform_id="generic", policy=restricted)
    ProjectConfigStore(project, config).save(config)

    for source in ("stanford-ai-api-gateway", "openai"):
        persist_deployment_profile(project, model_source=source, env={})
        policy = ProjectConfigStore(project, config).load().policy
        assert policy.allowed_data_destinations == ("institution-managed",), source
