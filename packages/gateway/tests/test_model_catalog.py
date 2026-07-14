# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

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
    custom_model_connection,
    load_model_connections,
)
from heartwood.gateway._model_catalog import _model_compatibility
from heartwood.schemas import PolicyProfile


def test_built_in_connections_are_non_secret_and_researcher_facing() -> None:
    connections = {
        connection.connection_id: connection for connection in BUILT_IN_MODEL_CONNECTIONS
    }

    assert set(connections) == {"anthropic", "custom-api", "local", "openai"}
    assert connections["local"].label == "Local"
    assert connections["custom-api"].description.startswith("A service")
    for connection in connections.values():
        connection.validate(configurable=connection.connection_id == "custom-api")
        serialized = connection.safe_dict({})
        assert "token" not in serialized
        assert "api_key" not in serialized


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
    ProjectConfigStore(
        denied_project,
        ProjectConfig(platform_id="generic", policy=local_only_policy),
    ).save(ProjectConfig(platform_id="generic", policy=local_only_policy))
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
    assert "https://second.example/v1/models" in config.policy.allowed_model_catalog_endpoints
    assert "https://first.example/v1/chat/completions" not in config.policy.allowed_model_endpoints
    assert "https://first.example/v1/models" not in config.policy.allowed_model_catalog_endpoints
    assert "HEARTWOOD_CUSTOM_MODEL_API_KEY" in config.policy.credential_allowlist
    assert "first-transient-secret" not in persisted
    assert "second-transient-secret" not in persisted


def test_managed_project_does_not_widen_policy_for_custom_api(tmp_path: Path) -> None:
    project = _project(tmp_path / "managed-custom")
    gateway = SessionGateway(
        project=project,
        env={"GOOGLE_PROJECT": "synthetic-terra-project"},
        model_catalog_service=ModelCatalogService(
            openai_lister=lambda _connection, _api_key: (ProviderModel("custom-coder"),)
        ),
    )

    with pytest.raises(ModelCatalogError, match="denied"):
        gateway.discover_models(
            "custom-api",
            token="transient-secret",
            base_url="https://custom.example/v1",
        )

    assert gateway.config_store.load().policy.policy_id != "generic-custom-api"
    assert not project.config_path.exists()


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
    ProjectConfigStore(
        project,
        ProjectConfig(platform_id="generic", policy=policy),
    ).save(ProjectConfig(platform_id="generic", policy=policy))
    return project


def _records(value: dict[str, object], key: str) -> list[dict[str, object]]:
    records = value.get(key)
    assert isinstance(records, list)
    assert all(isinstance(item, dict) for item in records)
    return cast(list[dict[str, object]], records)


def _record(value: dict[str, object], key: str) -> dict[str, object]:
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
        (manifest({**base, "connection_id": "local"}), "ids must be unique"),
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
