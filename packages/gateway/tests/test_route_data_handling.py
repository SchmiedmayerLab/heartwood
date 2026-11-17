# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Declared route data handling across setup, selection, session history and audit."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from heartwood.core_adapter import FileSessionStore
from heartwood.gateway import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalogError,
    ModelCatalogService,
    ModelConnection,
    ModelSettingsError,
    ProjectConfig,
    ProjectConfigError,
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


def _gateway(
    tmp_path: Path,
    *,
    deployment_policy: PolicyProfile | None = None,
    backend_id: str = "deterministic",
) -> SessionGateway:
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
        backend_id=backend_id,
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


def test_replacing_the_active_profile_applies_the_destination_restriction(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path, deployment_policy=_policy(allowed_data_destinations=("provider",)))
    try:
        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        gateway.connect_model("openai", "provider-model")
        active = gateway.settings_store.load().profile("openai")
        moved = replace(
            active,
            base_url="https://custom.example/v1",
            policy_endpoint="https://custom.example/v1/chat/completions",
        )
        with pytest.raises(ModelSettingsError, match="requires declared data handling"):
            gateway.save_model_profile(moved)
        assert gateway.settings_store.load().profile("openai") == active
        assert cast(dict[str, object], gateway.model_settings()["data_handling"])["route"] == (
            "OpenAI API"
        )
    finally:
        gateway.stop()


def test_profile_changes_that_keep_the_active_route_are_not_refused(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        deployment_policy=_policy(allowed_data_destinations=("provider", "institution-managed")),
    )
    try:
        gateway.discover_models("anthropic", token=_ANTHROPIC_TOKEN)
        gateway.connect_model("anthropic", "claude-synthetic")
        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        gateway.connect_model("openai", "provider-model")
        gateway.config_store.update(
            lambda config: replace(
                config,
                policy=config.policy.model_copy(
                    update={"allowed_data_destinations": ("institution-managed",)}
                ),
            )
        )
        with pytest.raises(ModelSettingsError, match="does not permit the provider"):
            gateway.select_model_profile("anthropic")
        remaining = gateway.remove_model_profile("anthropic")
        assert remaining["active_profile"] == "openai"
    finally:
        gateway.stop()


def test_profile_edits_and_selection_apply_the_full_route_policy(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, deployment_policy=_policy())
    try:
        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        gateway.connect_model("openai", "provider-model")
        active = gateway.settings_store.load().profile("openai")
        endpoint = "https://unlisted.example/v1/chat/completions"
        moved = replace(active, base_url="https://unlisted.example/v1", policy_endpoint=endpoint)
        with pytest.raises(ModelSettingsError, match="the policy does not permit this route"):
            gateway.save_model_profile(moved)
        assert gateway.settings_store.load().profile("openai") == active

        # Storing another profile is not a selection; selecting it is.
        gateway.save_model_profile(replace(moved, profile_id="unlisted"))
        with pytest.raises(ModelSettingsError, match="the policy does not permit this route"):
            gateway.select_model_profile("unlisted")
        assert gateway.model_settings()["active_profile"] == "openai"
    finally:
        gateway.stop()


def test_local_model_selection_applies_the_destination_restriction(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, deployment_policy=_policy(allowed_data_destinations=("provider",)))
    try:
        model_id = "qwen25-7b-instruct-q4_k_m"
        model_path = gateway.project.models_dir / model_id / "model.gguf"
        model_path.parent.mkdir(parents=True)
        model_path.write_bytes(b"synthetic")
        with pytest.raises(ProjectConfigError, match="does not permit the same-compute"):
            gateway.local_models.select_downloaded_local_model(
                model_id, model_path, "llama-cpp-cpu"
            )
        assert gateway.config_store.load().local_model is None
        assert gateway.model_settings()["active_profile"] is None
    finally:
        gateway.stop()


def test_a_new_deployment_destination_list_narrows_existing_projects(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, deployment_policy=_policy())
    try:
        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        gateway.connect_model("openai", "provider-model")
    finally:
        gateway.stop()

    gateway = _gateway(
        tmp_path, deployment_policy=_policy(allowed_data_destinations=("same-compute",))
    )
    try:
        assert gateway.config_store.load().policy.allowed_data_destinations == ("same-compute",)
        assert gateway.model_settings()["active_profile"] == "openai"
        validation = gateway.validate_model_profile()
        decision = cast(dict[str, object], validation["policy_decision"])
        assert decision["decision"] == "deny"
        gateway.handle(_command(CommandKind.CHAT, "after", prompt="Inspect the cohort"))
        replayed = FileSessionStore(gateway.sessions_root, "session-1").replay_events()
        assert [
            cast(dict[str, object], event.payload["decision"])["decision"]
            for event in replayed
            if event.kind == EventKind.MODEL_CALL_DECISION_RECORDED
        ] == ["deny"]
        # The deployment list is applied on load, not copied into project state.
        assert "allowed_data_destinations" not in gateway.project.config_path.read_text()
    finally:
        gateway.stop()

    gateway = _gateway(tmp_path, deployment_policy=_policy(allowed_data_destinations=("provider",)))
    try:
        assert gateway.config_store.load().policy.allowed_data_destinations == ("provider",)
        assert (
            cast(dict[str, object], gateway.validate_model_profile()["policy_decision"])["decision"]
            == "allow"
        )
    finally:
        gateway.stop()


def test_a_project_destination_list_stays_within_a_changed_deployment_list(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    narrowed = _policy(allowed_data_destinations=("provider",))
    config = ProjectConfig(platform_id="generic", policy=narrowed)
    ProjectConfigStore(project, config).save(config)

    wider = _policy(allowed_data_destinations=("provider", "same-compute"))
    store = ProjectConfigStore(project, config, deployment_policy=wider)
    assert store.load().policy.allowed_data_destinations == ("provider",)

    disjoint = _policy(allowed_data_destinations=("same-compute",))
    default = ProjectConfig(platform_id="generic", policy=disjoint)
    store = ProjectConfigStore(project, default, deployment_policy=disjoint)
    with pytest.raises(ProjectConfigError, match="allowed_data_destinations"):
        store.load()


def _readiness_check(gateway: SessionGateway, check_id: str) -> dict[str, object]:
    checks = cast(list[dict[str, object]], gateway.project_readiness()["checks"])
    return next(check for check in checks if check["check_id"] == check_id)


def test_readiness_reports_a_route_the_deployment_destinations_exclude(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, deployment_policy=_policy())
    try:
        gateway.discover_models("openai", token=_OPENAI_TOKEN)
        gateway.connect_model("openai", "provider-model")
    finally:
        gateway.stop()

    gateway = _gateway(
        tmp_path, deployment_policy=_policy(allowed_data_destinations=("same-compute",))
    )
    try:
        readiness = gateway.project_readiness()
        route = _readiness_check(gateway, "configuration-coherence")
        assert route["status"] == "fail"
        assert "deployment policy" in str(route["summary"])
        assert "data destination provider is not allowed" in str(route["summary"])
        assert readiness["state"] == "recovery-required"
        startup = gateway.startup_plan(interface="web")
        assert startup["phase"] == "recovery-required"
    finally:
        gateway.stop()


_LOCAL_ENDPOINT = "http://127.0.0.1:8765/v1/chat/completions"
_ALL_ENDPOINTS = (*_policy().allowed_model_endpoints, _LOCAL_ENDPOINT)


@pytest.mark.parametrize(
    "destinations", [None, ("provider",), ("same-compute",), ("institution-managed",)]
)
@pytest.mark.parametrize(
    "limits",
    [
        {},
        # The managed local and Anthropic endpoints are not allowlisted.
        {
            "allowed_model_endpoints": [
                "https://api.openai.com/v1/chat/completions",
                "https://custom.example/v1/chat/completions",
            ]
        },
        {"allowed_capability_tiers": ["experimental"]},
        {"credential_allowlist": ["HEARTWOOD_CUSTOM_MODEL_API_KEY", "OPENAI_API_KEY"]},
    ],
    ids=["all-routes", "endpoints", "capability-tiers", "credentials"],
)
def test_readiness_selection_and_calls_agree_on_every_route(
    tmp_path: Path, destinations: tuple[str, ...] | None, limits: dict[str, object]
) -> None:
    permissive = _policy(allowed_model_endpoints=_ALL_ENDPOINTS)
    ceiling = _policy(
        **{"allowed_model_endpoints": _ALL_ENDPOINTS, **limits},
        allowed_data_destinations=destinations,
    )
    for route in ("openai", "anthropic", "custom-api", "heartwood"):
        # Selection: a fresh project selects the route under the deployment policy.
        fresh = _gateway(tmp_path / route / "fresh", deployment_policy=ceiling)
        try:
            try:
                _select_route(fresh, route)
            except (ModelCatalogError, ModelSettingsError, ProjectConfigError):
                # Discovery applies the same policy, so a refused catalog refuses selection.
                selection_allows = False
            else:
                selection_allows = True
        finally:
            fresh.stop()

        # Readiness and the call: a route selected before the policy applied.
        project_root = tmp_path / route / "existing"
        earlier = _gateway(project_root, deployment_policy=permissive)
        try:
            _select_route(earlier, route)
        finally:
            earlier.stop()
        # The session's OpenHands backend makes the decision each model call receives;
        # no model is called.
        gateway = _gateway(project_root, deployment_policy=ceiling, backend_id="openhands")
        try:
            readiness = _readiness_check(gateway, "configuration-coherence")
            readiness_allows = readiness["status"] == "pass"
            assert readiness_allows or "deployment policy" in str(readiness["summary"])
            validation = gateway.validate_model_profile()["policy_decision"]
            validation_allows = cast(dict[str, object], validation)["decision"] == "allow"
            call = gateway._service("session-1").model_route_decision(
                decision_id="call-model-route", purpose="agent turn"
            )
            call_allows = call.decision == "allow"
        finally:
            gateway.stop()

        assert readiness_allows == selection_allows == validation_allows == call_allows, (
            route,
            destinations,
            limits,
        )


def _select_route(gateway: SessionGateway, route: str) -> None:
    if route == "heartwood":
        model_id = "qwen25-7b-instruct-q4_k_m"
        model_path = gateway.project.models_dir / model_id / "model.gguf"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_bytes(b"synthetic")
        gateway.local_models.select_downloaded_local_model(model_id, model_path, "llama-cpp-cpu")
    elif route == "custom-api":
        gateway.connect_model(
            "custom-api",
            "custom-coder",
            token="synthetic-custom-token",
            base_url="https://custom.example/v1",
            manual=True,
        )
    else:
        gateway.discover_models(
            route, token=_ANTHROPIC_TOKEN if route == "anthropic" else _OPENAI_TOKEN
        )
        gateway.connect_model(
            route, "claude-synthetic" if route == "anthropic" else "provider-model"
        )
