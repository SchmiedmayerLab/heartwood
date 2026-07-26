# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from heartwood.gateway import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalogError,
    ModelCatalogService,
    ModelConnection,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    ProviderModel,
    SessionGateway,
    SubscriptionDeviceLogin,
    SubscriptionError,
    custom_model_connection,
    load_model_connections,
)
from heartwood.gateway._model_catalog import _model_compatibility
from heartwood.schemas import PolicyProfile


class _SubscriptionProvider:
    connection_id = "openai-subscription"
    vendor = "openai"

    def __init__(self) -> None:
        self.available = False
        self.credential_checks = 0
        self.logged_in_model: str | None = None
        self.logged_out = False

    def models(self) -> tuple[str, ...]:
        return ("gpt-subscription",)

    def credential_available(self) -> bool:
        self.credential_checks += 1
        return self.available

    def login(
        self,
        *,
        model: str,
        force_login: bool,  # noqa: ARG002
        open_browser: bool,  # noqa: ARG002
        auth_method: Literal["browser", "device_code"],  # noqa: ARG002
    ) -> None:
        self.logged_in_model = model
        self.available = True

    def start_device_login(self) -> SubscriptionDeviceLogin:
        return SubscriptionDeviceLogin(
            login_id="login-1",
            connection_id=self.connection_id,
            verification_url="https://auth.example/device",
            user_code="TEST-CODE",
            poll_interval_seconds=2,
            status="pending",
        )

    def poll_device_login(self, login_id: str) -> SubscriptionDeviceLogin:
        assert login_id == "login-1"
        self.available = True
        return SubscriptionDeviceLogin(
            login_id=login_id,
            connection_id=self.connection_id,
            verification_url="https://auth.example/device",
            user_code="TEST-CODE",
            poll_interval_seconds=2,
            status="complete",
        )

    def logout(self) -> bool:
        self.available = False
        self.logged_out = True
        return True


def test_built_in_connections_are_non_secret_and_researcher_facing() -> None:
    connections = {
        connection.connection_id: connection for connection in BUILT_IN_MODEL_CONNECTIONS
    }

    assert set(connections) == {
        "anthropic",
        "custom-api",
        "heartwood",
        "openai",
        "openai-subscription",
    }
    assert connections["heartwood"].label == "Run with Heartwood"
    assert connections["heartwood"].group == "heartwood-managed"
    assert connections["openai"].group == "hosted-provider"
    assert connections["openai-subscription"].protocol == "subscription"
    assert connections["openai-subscription"].subscription_vendor == "openai"
    assert connections["openai-subscription"].credential_reference == "subscription:openai"
    assert connections["custom-api"].group == "compatible-service"
    assert connections["heartwood"].presentation_order < connections["openai"].presentation_order
    assert connections["custom-api"].description.startswith("A service")
    for connection in connections.values():
        connection.validate(configurable=connection.connection_id == "custom-api")
        serialized = connection.safe_dict({})
        assert serialized["group_label"]
        assert "token" not in serialized
        assert "api_key" not in serialized


def test_subscription_login_uses_shared_catalog_policy_and_profile(tmp_path: Path) -> None:
    provider = _SubscriptionProvider()
    gateway = SessionGateway(
        project=_project(tmp_path),
        env={},
        backend_id="deterministic",
        subscription_provider=provider,
        model_catalog_service=ModelCatalogService(
            subscription_lister=lambda _connection, _api_key: (ProviderModel("gpt-subscription"),),
        ),
    )

    gateway.configure_model_source("openai-subscription")
    catalog = gateway.discover_models("openai-subscription", refresh=True)
    assert cast(dict[str, object], catalog["connection"])["credential_status"] == "missing"
    assert cast(list[dict[str, object]], catalog["models"])[0]["model_id"] == "gpt-subscription"

    started = gateway.start_subscription_device_login("openai-subscription")
    assert started["status"] == "pending"
    completed = gateway.poll_subscription_device_login("openai-subscription", "login-1")
    assert completed["status"] == "complete"

    settings = gateway.connect_model("openai-subscription", "gpt-subscription")
    profile = cast(list[dict[str, object]], settings["profiles"])[0]
    assert profile["auth_type"] == "subscription"
    assert profile["subscription_vendor"] == "openai"
    assert profile["credential_status"] == "available"
    provider.credential_checks = 0
    settings = gateway.model_settings()
    assert provider.credential_checks == 1
    assert (
        cast(list[dict[str, object]], settings["profiles"])[0]["credential_status"] == "available"
    )
    subscription_connection = next(
        connection
        for connection in cast(list[dict[str, object]], settings["connections"])
        if connection["connection_id"] == "openai-subscription"
    )
    assert subscription_connection["credential_status"] == "available"
    validation = gateway.validate_model_profile()
    assert validation["credential_status"] == "available"
    assert cast(dict[str, object], validation["policy_decision"])["decision"] == "allow"

    gateway.forget_credential("openai-subscription")
    assert provider.logged_out
    profiles = cast(list[dict[str, object]], gateway.model_settings()["profiles"])
    assert profiles[0]["credential_status"] == "missing"


def test_subscription_gateway_fails_closed_at_the_openhands_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SubscriptionProvider()
    gateway = SessionGateway(
        project=_project(tmp_path),
        env={},
        backend_id="deterministic",
        subscription_provider=provider,
    )

    with pytest.raises(ModelCatalogError, match="unknown model connection"):
        gateway.login_subscription("missing", model_id="gpt-subscription")
    with pytest.raises(ModelCatalogError, match="does not support account sign-in"):
        gateway.login_subscription("openai", model_id="gpt-subscription")
    with pytest.raises(ModelCatalogError, match="no forgettable credential"):
        gateway.forget_credential("heartwood")
    with pytest.raises(ModelCatalogError, match="unsupported subscription login method"):
        gateway.login_subscription(
            "openai-subscription",
            model_id="gpt-subscription",
            auth_method=cast(Any, "password"),
        )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise SubscriptionError("OpenHands subscription unavailable")

    monkeypatch.setattr(provider, "login", fail)
    with pytest.raises(ModelCatalogError, match="subscription unavailable"):
        gateway.login_subscription("openai-subscription", model_id="gpt-subscription")

    monkeypatch.setattr(provider, "start_device_login", fail)
    with pytest.raises(ModelCatalogError, match="subscription unavailable"):
        gateway.start_subscription_device_login("openai-subscription")

    monkeypatch.setattr(provider, "poll_device_login", fail)
    with pytest.raises(ModelCatalogError, match="subscription unavailable"):
        gateway.poll_subscription_device_login("openai-subscription", "login-1")

    monkeypatch.setattr(provider, "logout", fail)
    with pytest.raises(ModelCatalogError, match="subscription unavailable"):
        gateway.forget_credential("openai-subscription")

    monkeypatch.setattr(provider, "credential_available", fail)
    connections = cast(list[dict[str, object]], gateway.model_settings()["connections"])
    subscription = next(
        item for item in connections if item["connection_id"] == "openai-subscription"
    )
    assert subscription["credential_status"] == "missing"


def test_platform_connection_manifest_supports_multi_model_research_service(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.model-connections.v1",
                "connections": [
                    {
                        "connection_id": "research-ai",
                        "label": "Research AI Service",
                        "protocol": "static",
                        "model_prefix": "litellm_proxy/",
                        "source": "platform",
                        "credential_kind": "managed-identity",
                        "policy_endpoint": "https://models.example/v1/chat/completions",
                        "catalog_endpoint": None,
                        "description": "Models authorized by the research environment.",
                        "static_models": ["coding-large", "coding-small"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    connections = load_model_connections(path)
    research = next(item for item in connections if item.connection_id == "research-ai")

    assert research.label == "Research AI Service"
    assert research.static_models == ("coding-large", "coding-small")
    assert research.source == "platform"
    assert research.group == "research-environment"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": "wrong", "connections": []}, "schema"),
        (
            {
                "schema_version": "heartwood.model-connections.v1",
                "connections": {"bad": "shape"},
            },
            "must be a list",
        ),
        (
            {
                "schema_version": "heartwood.model-connections.v1",
                "connections": [{"token": "must-not-persist"}],
            },
            "inline secret",
        ),
    ],
)
def test_connection_manifest_rejects_invalid_or_secret_state(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelCatalogError, match=message):
        load_model_connections(path)


def test_connection_manifest_accepts_only_platform_sources(tmp_path: Path) -> None:
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.model-connections.v1",
                "connections": [
                    {
                        "connection_id": "misclassified",
                        "label": "Misclassified Service",
                        "protocol": "static",
                        "model_prefix": "openai/",
                        "source": "user",
                        "credential_kind": "managed-identity",
                        "policy_endpoint": "https://models.example/v1/chat/completions",
                        "catalog_endpoint": None,
                        "static_models": ["model"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelCatalogError, match="must use source platform"):
        load_model_connections(path)


def test_catalog_normalizes_exact_ids_sorts_status_and_caches() -> None:
    calls = 0

    def list_models(
        _connection: ModelConnection,
        _api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        nonlocal calls
        calls += 1
        return (
            ProviderModel("embedding-only", "Embedding"),
            ProviderModel("verified", "Verified"),
            ProviderModel("unknown", "Unknown"),
            ProviderModel("blank-name", " "),
            ProviderModel("verified", "Duplicate"),
            ProviderModel("invalid model"),
        )

    def compatibility(
        _connection: ModelConnection,
        model: str,
    ) -> tuple[str, str, int | None, bool | None]:
        if model.endswith("verified"):
            return "available", "verified", 128_000, True
        if model.endswith("embedding-only"):
            return "unsupported", "embedding", None, False
        return "experimental", "unknown", None, None

    service = ModelCatalogService(
        openai_lister=list_models,
        compatibility=compatibility,  # type: ignore[arg-type]
    )
    connection = _openai_connection()

    first = service.discover(connection, api_key="secret")
    second = service.discover(connection, api_key="different-secret")
    refreshed = service.discover(connection, api_key="secret", refresh=True)

    assert calls == 2
    assert second is first
    assert refreshed is not first
    assert [model.model_id for model in first.models] == [
        "verified",
        "blank-name",
        "unknown",
        "embedding-only",
    ]
    assert first.models[0].execution_model == "openai/verified"
    assert first.models[1].display_name == "blank-name"
    assert "secret" not in str(first.safe_dict({}))
    assert service.cached("openai") is refreshed
    service.invalidate("openai")
    assert service.cached("openai") is None


def test_catalog_cache_expires_before_direct_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr(
        "heartwood.gateway._model_catalog.time.monotonic",
        lambda: now,
    )
    service = ModelCatalogService(
        openai_lister=lambda _connection, _api_key: (ProviderModel("model"),),
        cache_ttl_seconds=5,
    )

    catalog = service.discover(_openai_connection(), api_key="secret")
    assert service.cached("openai") is catalog
    now = 5.0
    assert service.cached("openai") is None


def test_official_sdk_listers_iterate_all_returned_pages_and_preserve_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    closed: list[bool] = []

    class Models:
        def list(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(id="model-a", display_name="Model A"),
                SimpleNamespace(id="model-b", display_name="Model B"),
            ]

    class Client:
        def __init__(self, **options: object) -> None:
            created.append(options)
            self.models = Models()

        def close(self) -> None:
            closed.append(True)

    def fake_import(name: str) -> SimpleNamespace:
        if name == "openai":
            return SimpleNamespace(OpenAI=Client)
        if name == "anthropic":
            return SimpleNamespace(Anthropic=Client)
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("heartwood.gateway._model_catalog.import_module", fake_import)

    def compatibility(
        _connection: ModelConnection,
        _model: str,
    ) -> tuple[str, str, None, bool]:
        return "available", "verified", None, True

    service = ModelCatalogService(compatibility=compatibility)  # type: ignore[arg-type]

    openai = service.discover(_openai_connection(), api_key="openai-secret", refresh=True)
    anthropic = service.discover(
        _anthropic_connection(),
        api_key="anthropic-secret",
        refresh=True,
    )

    assert [model.model_id for model in openai.models] == ["model-a", "model-b"]
    assert [model.display_name for model in anthropic.models] == ["Model A", "Model B"]
    assert created == [
        {"api_key": "openai-secret", "max_retries": 1, "timeout": 30.0},
        {"api_key": "anthropic-secret", "max_retries": 1, "timeout": 30.0},
    ]
    assert closed == [True, True]


def test_provider_failures_are_content_minimized() -> None:
    class AuthenticationFailureError(RuntimeError):
        pass

    def fail(
        _connection: ModelConnection,
        _api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        raise AuthenticationFailureError("response included a secret")

    service = ModelCatalogService(openai_lister=fail)

    with pytest.raises(ModelCatalogError, match="rejected") as caught:
        service.discover(_openai_connection(), api_key="private-token")

    assert "secret" not in str(caught.value)
    assert "private-token" not in str(caught.value)


def test_custom_api_requires_https_or_loopback_and_manual_is_scoped() -> None:
    loopback = custom_model_connection("http://127.0.0.1:9000/v1", has_token=False)
    remote = custom_model_connection("https://models.example/v1", has_token=True)
    service = ModelCatalogService(
        compatibility=lambda _connection, _model: ("experimental", "unknown", None, None)
    )

    manual = service.manual(loopback, "custom-coder")

    assert manual.models[0].execution_model == "openai/custom-coder"
    assert remote.api_key_env == "HEARTWOOD_CUSTOM_MODEL_API_KEY"
    with pytest.raises(ModelCatalogError, match="requires a token"):
        custom_model_connection("https://models.example/v1", has_token=False)
    with pytest.raises(ModelCatalogError, match="require HTTPS"):
        custom_model_connection("http://models.example/v1", has_token=True)
    with pytest.raises(ModelCatalogError, match="invalid port"):
        custom_model_connection("https://models.example:invalid/v1", has_token=True)
    with pytest.raises(ModelCatalogError, match="invalid port"):
        custom_model_connection("https://models.example:0/v1", has_token=True)
    with pytest.raises(ModelCatalogError, match="only for Custom API"):
        service.manual(_openai_connection(), "model")


def test_gateway_discovers_all_platform_models_and_materializes_one_profile(
    tmp_path: Path,
) -> None:
    research = ModelConnection(
        connection_id="research-ai",
        label="Research AI Service",
        protocol="static",
        model_prefix="litellm_proxy/",
        source="platform",
        credential_kind="managed-identity",
        catalog_endpoint=None,
        policy_endpoint="https://models.example/v1/chat/completions",
        aws_region_name="us-west-2",
        aws_profile_name="research-runtime",
        static_models=("coding-large", "coding-small"),
    )
    service = ModelCatalogService(
        compatibility=lambda _connection, _model: ("available", "verified", None, True)
    )
    gateway = SessionGateway(
        project=_catalog_project(tmp_path),
        env={},
        model_connections=(*BUILT_IN_MODEL_CONNECTIONS, research),
        model_catalog_service=service,
    )

    catalog = gateway.discover_models("research-ai")
    settings = gateway.connect_model("research-ai", "coding-small")
    catalog_models = _records(catalog, "models")
    profiles = _records(settings, "profiles")

    assert [model["model_id"] for model in catalog_models] == [
        "coding-large",
        "coding-small",
    ]
    assert settings["active_profile"] == "research-ai"
    profile = next(item for item in profiles if item["profile_id"] == "research-ai")
    assert profile["model"] == "litellm_proxy/coding-small"
    assert profile["credential_kind"] == "managed-identity"
    assert profile["aws_region_name"] == "us-west-2"
    assert profile["aws_profile_name"] == "research-runtime"


def test_gateway_authorizes_discovery_before_retaining_transient_token(
    tmp_path: Path,
) -> None:
    captured: list[str | None] = []

    def lister(
        _connection: ModelConnection,
        api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        captured.append(api_key)
        return (ProviderModel("provider-model"),)

    service = ModelCatalogService(
        openai_lister=lister,
        compatibility=lambda _connection, _model: ("available", "verified", None, True),
    )
    denied_project = _project(tmp_path / "denied")
    local_only_policy = PolicyProfile(
        policy_id="local-only-test",
        platform_id="generic",
        allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
        allowed_model_catalog_endpoints=("http://127.0.0.1:8765/v1/models",),
        allowed_capability_tiers=("supervised", "experimental"),
        allowed_action_confirmation_modes=("always-confirm",),
        credential_allowlist=(),
    )
    denied_config = ProjectConfig(platform_id="generic", policy=local_only_policy)
    ProjectConfigStore(denied_project, denied_config).save(denied_config)
    denied = SessionGateway(
        project=denied_project,
        env={},
        model_catalog_service=service,
    )

    with pytest.raises(ModelCatalogError, match="denied"):
        denied.discover_models("openai", token="must-not-survive")
    assert _records(denied.model_settings(), "connections")[1]["credential_status"] == "missing"

    allowed = SessionGateway(
        project=_catalog_project(tmp_path / "allowed"),
        env={},
        model_catalog_service=service,
    )
    catalog = allowed.discover_models("openai", token="transient-secret")
    settings = allowed.connect_model("openai", "provider-model")
    persisted = (tmp_path / "allowed" / ".heartwood" / "config.toml").read_text(encoding="utf-8")

    assert captured == ["transient-secret"]
    assert _record(catalog, "connection")["credential_status"] == "available"
    assert settings["active_profile"] == "openai"
    assert "transient-secret" not in persisted
    assert "transient-secret" not in str(settings)
    allowed.stop()
    assert _records(allowed.model_settings(), "connections")[1]["credential_status"] == "missing"


def test_custom_api_manual_fallback_reuses_the_authorized_runtime_credential(
    tmp_path: Path,
) -> None:
    attempts = 0

    def unavailable(
        _connection: ModelConnection,
        _api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("catalog route is not implemented")

    service = ModelCatalogService(
        openai_lister=unavailable,
        compatibility=lambda _connection, _model: ("experimental", "unknown", None, None),
    )
    gateway = SessionGateway(
        project=_catalog_project(tmp_path / "custom"),
        env={},
        model_catalog_service=service,
    )
    base_url = "https://custom.example/v1"

    with pytest.raises(ModelCatalogError, match="catalog is unavailable"):
        gateway.discover_models(
            "custom-api",
            token="transient-custom-secret",
            base_url=base_url,
        )
    settings = gateway.connect_model(
        "custom-api",
        "custom-coder",
        base_url=base_url,
        manual=True,
    )

    assert attempts == 1
    assert settings["active_profile"] == "custom-api"
    assert _records(settings, "profiles")[0]["model"] == "openai/custom-coder"
    assert "transient-custom-secret" not in str(settings)
    assert "transient-custom-secret" not in (
        tmp_path / "custom" / ".heartwood" / "config.toml"
    ).read_text(encoding="utf-8")
    with pytest.raises(ModelCatalogError, match="requires a token"):
        gateway.discover_models(
            "custom-api",
            base_url="https://other.example/v1",
        )


def test_generic_project_authorizes_only_the_selected_custom_api_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lister(
        _connection: ModelConnection,
        _api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        return (ProviderModel("custom-coder"),)

    project = _project(tmp_path / "generic-custom")
    gateway = SessionGateway(
        project=project,
        env={},
        model_catalog_service=ModelCatalogService(
            openai_lister=lister,
            compatibility=lambda _connection, _model: (
                "experimental",
                "unknown",
                None,
                None,
            ),
        ),
    )

    first = gateway.discover_models(
        "custom-api",
        token="first-transient-secret",
        base_url="https://first.example/v1",
    )
    second = gateway.discover_models(
        "custom-api",
        token="second-transient-secret",
        base_url="https://second.example/v1",
    )
    config_save_calls = 0
    original_save = gateway.config_store.save

    def track_config_save(config: ProjectConfig) -> None:
        nonlocal config_save_calls
        config_save_calls += 1
        original_save(config)

    monkeypatch.setattr(gateway.config_store, "save", track_config_save)
    repeated = gateway.discover_models(
        "custom-api",
        token="second-transient-secret",
        base_url="https://second.example/v1",
    )
    config = gateway.config_store.load()
    persisted = project.config_path.read_text(encoding="utf-8")

    assert _records(first, "models")[0]["model_id"] == "custom-coder"
    assert _records(second, "models")[0]["model_id"] == "custom-coder"
    assert _records(repeated, "models")[0]["model_id"] == "custom-coder"
    assert config_save_calls == 0
    assert config.policy.policy_id == "generic-custom-api"
    assert "https://second.example/v1/chat/completions" in config.policy.allowed_model_endpoints
    assert "https://second.example/v1/responses" in config.policy.allowed_model_endpoints
    assert "https://second.example/v1/models" in config.policy.allowed_model_catalog_endpoints
    assert "https://first.example/v1/chat/completions" not in config.policy.allowed_model_endpoints
    assert "https://first.example/v1/responses" not in config.policy.allowed_model_endpoints
    assert "https://first.example/v1/models" not in config.policy.allowed_model_catalog_endpoints
    assert "HEARTWOOD_CUSTOM_MODEL_API_KEY" in config.policy.credential_allowlist
    assert "first-transient-secret" not in persisted
    assert "second-transient-secret" not in persisted


def test_terra_custom_api_uses_an_explicit_project_policy(tmp_path: Path) -> None:
    project = _project(tmp_path / "managed-custom")
    gateway = SessionGateway(
        project=project,
        env={"GOOGLE_PROJECT": "synthetic-terra-project"},
        model_catalog_service=ModelCatalogService(
            openai_lister=lambda _connection, _api_key: (ProviderModel("custom-coder"),)
        ),
    )

    gateway.discover_models(
        "custom-api",
        token="transient-secret",
        base_url="https://custom.example/v1",
    )

    policy = gateway.config_store.load().policy
    assert policy.policy_id == "terra-custom-api"
    assert "https://custom.example/v1/chat/completions" in policy.allowed_model_endpoints


def _openai_connection() -> ModelConnection:
    return next(
        connection
        for connection in BUILT_IN_MODEL_CONNECTIONS
        if connection.connection_id == "openai"
    )


def _anthropic_connection() -> ModelConnection:
    return next(
        connection
        for connection in BUILT_IN_MODEL_CONNECTIONS
        if connection.connection_id == "anthropic"
    )


def _project(root: Path) -> ProjectContext:
    root.mkdir(parents=True, exist_ok=True)
    return ProjectContext(root)


def _catalog_project(root: Path) -> ProjectContext:
    project = _project(root)
    policy = PolicyProfile(
        policy_id="catalog-test",
        platform_id="generic",
        allowed_model_endpoints=(
            "https://api.openai.com/v1/chat/completions",
            "https://custom.example/v1/chat/completions",
            "https://models.example/v1/chat/completions",
        ),
        allowed_model_catalog_endpoints=(
            "https://api.openai.com/v1/models",
            "https://custom.example/v1/models",
        ),
        allowed_capability_tiers=("supervised", "experimental"),
        allowed_action_confirmation_modes=("always-confirm", "confirm-risky"),
        credential_allowlist=(
            "HEARTWOOD_CUSTOM_MODEL_API_KEY",
            "OPENAI_API_KEY",
            "managed-identity",
        ),
    )
    config = ProjectConfig(platform_id="generic", policy=policy)
    ProjectConfigStore(project, config).save(config)
    return project


def _records(value: Mapping[str, object], key: str) -> list[dict[str, object]]:
    records = value.get(key)
    assert isinstance(records, list)
    assert all(isinstance(item, dict) for item in records)
    return cast(list[dict[str, object]], records)


def _record(value: Mapping[str, object], key: str) -> dict[str, object]:
    record = value.get(key)
    assert isinstance(record, dict)
    return cast(dict[str, object], record)


def _validation_connection() -> ModelConnection:
    return ModelConnection(
        connection_id="validation",
        label="Validation",
        protocol="openai-compatible",
        model_prefix="openai/",
        source="platform",
        credential_kind="environment",
        api_key_env="MODEL_API_KEY",
        base_url="https://models.example/v1",
        catalog_endpoint="https://models.example/v1/models",
        policy_endpoint="https://models.example/v1/chat/completions",
    )


@pytest.mark.parametrize(
    ("connection", "configurable", "message"),
    [
        (replace(_validation_connection(), connection_id="bad id"), False, "connection_id"),
        (replace(_validation_connection(), label=" "), False, "label"),
        (replace(_validation_connection(), model_prefix="openai"), False, "model_prefix"),
        (replace(_validation_connection(), api_version=" "), False, "api_version"),
        (replace(_validation_connection(), policy_endpoint=None), False, "policy_endpoint"),
        (replace(_validation_connection(), catalog_endpoint=None), False, "catalog_endpoint"),
        (
            replace(_validation_connection(), base_url="https://other.example/v1"),
            False,
            "same origin",
        ),
        (replace(_validation_connection(), api_key_env="bad name"), False, "api_key_env"),
        (
            replace(
                _validation_connection(),
                credential_kind="file",
                api_key_env=None,
                api_key_file="relative-secret",
            ),
            False,
            "absolute",
        ),
        (
            replace(_validation_connection(), credential_kind="managed-identity"),
            False,
            "cannot declare",
        ),
        (
            replace(
                _validation_connection(),
                credential_kind="none",
                api_key_env=None,
            ),
            False,
            "loopback",
        ),
        (
            replace(
                _validation_connection(),
                protocol="static",
                catalog_endpoint=None,
            ),
            False,
            "at least one model",
        ),
        (
            replace(_validation_connection(), static_models=("model",)),
            False,
            "only allowed",
        ),
        (
            replace(
                _validation_connection(),
                protocol="static",
                catalog_endpoint=None,
                static_models=("bad model",),
            ),
            False,
            "without whitespace",
        ),
        (
            replace(
                _validation_connection(),
                protocol="static",
                catalog_endpoint=None,
                static_models=("model", "model"),
            ),
            False,
            "unique",
        ),
        (_validation_connection(), True, "cannot declare fixed endpoints"),
    ],
)
def test_model_connection_validation_rejects_ambiguous_or_unsafe_configuration(
    connection: ModelConnection,
    configurable: bool,
    message: str,
) -> None:
    with pytest.raises(ModelCatalogError, match=message):
        connection.validate(configurable=configurable)


def test_model_connection_resolves_environment_file_and_managed_credentials(
    tmp_path: Path,
) -> None:
    environment = _validation_connection()
    assert environment.credential_status({}) == "missing"
    assert environment.credential_status({"MODEL_API_KEY": "secret"}) == "available"
    assert environment.resolve_api_key({"MODEL_API_KEY": "secret"}) == "secret"
    with pytest.raises(ModelCatalogError, match="unavailable"):
        environment.resolve_api_key({})

    secret_file = tmp_path / "model-token"
    file_connection = replace(
        environment,
        credential_kind="file",
        api_key_env=None,
        api_key_file=str(secret_file),
    )
    file_connection.validate()
    assert file_connection.credential_status({}) == "missing"
    with pytest.raises(ModelCatalogError, match="unavailable"):
        file_connection.resolve_api_key({})
    secret_file.write_text("file-secret\n", encoding="utf-8")
    assert file_connection.credential_status({}) == "available"
    assert file_connection.resolve_api_key({}) == "file-secret"
    secret_file.write_text("", encoding="utf-8")
    with pytest.raises(ModelCatalogError, match="empty"):
        file_connection.resolve_api_key({})

    managed = replace(
        environment,
        credential_kind="managed-identity",
        api_key_env=None,
    )
    managed.validate()
    assert managed.credential_reference == "managed-identity"
    assert managed.credential_status({}) == "configured"
    assert managed.resolve_api_key({}) is None


def test_manifest_rejects_unreadable_duplicate_and_malformed_connections(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ModelCatalogError, match="unable to load"):
        load_model_connections(missing)

    def manifest(connection: object) -> dict[str, object]:
        return {
            "schema_version": "heartwood.model-connections.v1",
            "connections": [connection],
        }

    base: dict[str, object] = {
        "connection_id": "research",
        "label": "Research",
        "protocol": "static",
        "model_prefix": "openai/",
        "source": "platform",
        "credential_kind": "managed-identity",
        "policy_endpoint": "https://models.example/v1/chat/completions",
        "catalog_endpoint": None,
        "static_models": ["model"],
    }
    cases = (
        (manifest("bad"), "must be an object"),
        (manifest({**base, "unsupported": True}), "unsupported fields"),
        (manifest({**base, "static_models": "model"}), "list of strings"),
        (manifest({**base, "description": 7}), "description"),
        (manifest({**base, "connection_id": "heartwood"}), "ids must be unique"),
    )
    for index, (payload, message) in enumerate(cases):
        path = tmp_path / f"invalid-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ModelCatalogError, match=message):
            load_model_connections(path)


def test_compatibility_uses_openhands_and_litellm_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    monkeypatch.delenv("OPENHANDS_SUPPRESS_BANNER", raising=False)

    class LiteLlm:
        @staticmethod
        def get_model_info(*, model: str) -> dict[str, object]:
            if model.endswith("embedding"):
                return {"mode": "embedding", "max_input_tokens": 32_768}
            if model.endswith("short"):
                return {"mode": "chat", "max_input_tokens": 8_192}
            return {"mode": "chat", "max_input_tokens": 32_768}

        @staticmethod
        def supports_function_calling(*, model: str) -> bool:
            if model.endswith("unknown-tools"):
                raise RuntimeError("metadata unavailable")
            return True

    def fake_import(name: str) -> object:
        if name == "openhands.sdk.llm.utils.verified_models":
            return SimpleNamespace(VERIFIED_MODELS={"openai": ["openai/verified"]})
        if name == "litellm":
            return LiteLlm
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("heartwood.gateway._model_catalog.import_module", fake_import)
    connection = _openai_connection()

    assert _model_compatibility(connection, "openai/verified")[0] == "available"
    assert _model_compatibility(connection, "openai/embedding")[0] == "unsupported"
    short = _model_compatibility(connection, "openai/short")
    assert short[0] == "experimental"
    assert short[2] == 8_192
    experimental = _model_compatibility(connection, "openai/unknown-tools")
    assert experimental[0] == "experimental"
    assert experimental[3] is None
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["OPENHANDS_SUPPRESS_BANNER"] == "1"


def test_stanford_gateway_uses_openhands_model_compatibility_and_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ModelConnection(
        connection_id="stanford-ai-api-gateway",
        label="Stanford AI API Gateway",
        protocol="openai-compatible",
        model_prefix="openai/",
        source="platform",
        credential_kind="environment",
        api_key_env="STANFORD_AI_API_KEY",
        base_url="https://aiapi-prod.stanford.edu/v1",
        catalog_endpoint="https://aiapi-prod.stanford.edu/v1/models",
        policy_endpoint="https://aiapi-prod.stanford.edu/v1/chat/completions",
    )
    monkeypatch.setattr(
        "heartwood.gateway._model_catalog._verified_openhands_models",
        lambda _connection: {"openai/gpt-5.4"},
    )

    availability, reason, context_window, supports_tools = _model_compatibility(
        connection,
        "openai/gpt-5.4",
    )

    assert availability == "available"
    assert reason == "Verified by the pinned OpenHands SDK"
    assert context_window is None
    assert supports_tools is True
    assert connection.request_endpoint("openai/gpt-5.4") == (
        "https://aiapi-prod.stanford.edu/v1/responses"
    )
    assert connection.request_endpoint("openai/claude-opus-4-7") == (
        "https://aiapi-prod.stanford.edu/v1/chat/completions"
    )


def test_request_endpoint_uses_openhands_model_capabilities() -> None:
    connections = {
        connection.connection_id: connection for connection in BUILT_IN_MODEL_CONNECTIONS
    }

    assert connections["openai"].request_endpoint("openai/gpt-5.4") == (
        "https://api.openai.com/v1/responses"
    )
    assert connections["openai"].request_endpoint("openai/gpt-4.1") == (
        "https://api.openai.com/v1/chat/completions"
    )
    assert connections["anthropic"].request_endpoint("anthropic/claude-current") == (
        "https://api.anthropic.com/v1/messages"
    )
    assert connections["openai-subscription"].request_endpoint("openai/gpt-5.4") == (
        "https://chatgpt.com/backend-api/codex/responses"
    )
