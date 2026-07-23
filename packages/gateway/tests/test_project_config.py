# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import tomllib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from typing import Literal

import pytest

from heartwood.adapters.platform import select_platform_adapter
from heartwood.gateway import (
    ActionSettings,
    LocalModelSelection,
    ModelConnection,
    ModelProfile,
    ModelSettings,
    ProjectConfig,
    ProjectConfigError,
    ProjectConfigStore,
    ProjectContext,
)
from heartwood.gateway._project_config import _config_mapping, project_config_from_mapping


def _default_config(project: ProjectContext) -> ProjectConfig:
    policy = select_platform_adapter({}).default_policy_profile()
    config = ProjectConfig(platform_id="generic", policy=policy)
    config.validate(project)
    return config


def test_project_config_round_trip_uses_one_toml_file(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    store = ProjectConfigStore(project, _default_config(project))
    profile = ModelProfile(
        profile_id="heartwood",
        model="openai/heartwood-managed-model",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
    )
    configured = ProjectConfig(
        platform_id="generic",
        model_source="heartwood",
        policy=select_platform_adapter({}).default_policy_profile(),
        model_settings=ModelSettings(
            active_profile="heartwood",
            profiles=(profile,),
        ),
    )

    store.save(configured)

    assert store.load() == configured
    assert project.config_path.stat().st_mode & 0o777 == 0o600
    with project.config_path.open("rb") as file:
        persisted = tomllib.load(file)
    assert persisted["model_source"] == "heartwood"
    assert persisted["models"]["active_profile"] == "heartwood"
    assert not (project.state_root / "models.json").exists()
    assert not (project.state_root / "actions.json").exists()


def test_project_config_store_returns_unsaved_default(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    default = _default_config(project)
    store = ProjectConfigStore(project, default)

    assert store.load() == default
    assert not project.state_root.exists()
    assert not store.configured


def test_project_config_updates_serialize_across_store_instances(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    first = ProjectConfigStore(project, _default_config(project))
    second = ProjectConfigStore(project, _default_config(project))
    profile = ModelProfile(
        profile_id="heartwood",
        model="openai/heartwood-managed-model",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
    )
    entered = Event()
    release = Event()

    def update_model(config: ProjectConfig) -> ProjectConfig:
        entered.set()
        assert release.wait(timeout=2)
        return config.with_model_settings(
            ModelSettings(active_profile=profile.profile_id, profiles=(profile,))
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        model_update = executor.submit(first.update, update_model)
        assert entered.wait(timeout=2)
        action_update = executor.submit(
            second.update,
            lambda config: config.with_action_settings(
                ActionSettings(confirmation_mode="confirm-risky")
            ),
        )
        release.set()
        model_update.result(timeout=2)
        action_update.result(timeout=2)

    configured = first.load()
    assert configured.model_settings.active_profile == profile.profile_id
    assert configured.action_settings.confirmation_mode == "confirm-risky"
    assert project.config_lock_path.stat().st_mode & 0o777 == 0o600


def test_local_model_selection_stays_under_project_model_root(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    model = project.models_dir / "reviewed" / "model.gguf"
    model.parent.mkdir()
    model.write_bytes(b"synthetic")
    store = ProjectConfigStore(project, _default_config(project))

    configured = store.select_local_model(artifact_id="reviewed", path=model)

    assert configured.local_model == LocalModelSelection(
        artifact_id="reviewed",
        path=".heartwood/models/reviewed/model.gguf",
    )
    assert configured.model_source == "heartwood"
    assert configured.local_model.resolved_path(project) == model
    with pytest.raises(ProjectConfigError, match=r"under \.heartwood/models"):
        store.select_local_model(artifact_id="outside", path=tmp_path / "outside.gguf")


def test_project_config_selects_model_source_and_settings_atomically(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    store = ProjectConfigStore(project, _default_config(project))
    profile = ModelProfile(
        profile_id="provider_connection",
        model="openai/model",
        policy_endpoint="https://api.openai.com/v1/chat/completions",
        credential_kind="environment",
        api_key_env="OPENAI_API_KEY",
    )
    settings = ModelSettings(
        active_profile=profile.profile_id,
        profiles=(profile,),
    )

    configured = store.select_model_source(profile.profile_id, settings)

    assert configured.model_source == "provider_connection"
    assert configured.model_settings == settings
    assert store.load() == configured


def test_project_config_selects_local_model_and_profile_atomically(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    model = project.models_dir / "reviewed" / "model.gguf"
    model.parent.mkdir()
    model.write_bytes(b"synthetic")
    store = ProjectConfigStore(project, _default_config(project))
    profile = ModelProfile(
        profile_id="heartwood",
        model="openai/heartwood-managed-model",
        base_url="http://127.0.0.1:8765/v1",
        policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
        credential_kind="none",
    )
    settings = ModelSettings(active_profile="heartwood", profiles=(profile,))

    configured = store.select_local_model(
        artifact_id="reviewed",
        path=model,
        settings=settings,
    )

    assert configured.model_source == "heartwood"
    assert configured.local_model is not None
    assert configured.model_settings == settings
    assert store.load() == configured


def test_project_config_persists_user_selected_model_provenance(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    model = project.models_dir / "hf-model" / "model-q4_k_m.gguf"
    model.parent.mkdir()
    model.write_bytes(b"synthetic")
    store = ProjectConfigStore(project, _default_config(project))

    configured = store.select_local_model(
        artifact_id="hf-model",
        path=model,
        runtime="llama-cpp",
        display_name="Research Model Q4_K_M",
        source_repository="example/research-model-gguf",
        source_revision="1" * 40,
        source_path="model-q4_k_m.gguf",
        model_type="qwen2",
        size_bytes=9,
        minimum_free_bytes=9,
        license_posture="Source model card reports apache-2.0.",
        artifact_sha256="a" * 64,
        minimum_resource_envelope="Estimated minimum resources",
        recommended_resource_envelope="Recommended resources",
        catalog_source="user-selected",
    )

    assert configured.local_model is not None
    assert configured.local_model.catalog_source == "user-selected"
    assert configured.local_model.source_repository == "example/research-model-gguf"
    assert configured.local_model.model_type == "qwen2"
    assert configured.local_model.artifact_sha256 == "a" * 64
    assert configured.local_model.recommended_resource_envelope == "Recommended resources"
    assert store.load().local_model == configured.local_model


def test_project_config_rejects_absolute_local_model_path(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    config = _default_config(project)
    invalid = ProjectConfig(
        platform_id=config.platform_id,
        policy=config.policy,
        local_model=LocalModelSelection(artifact_id="bad", path=str(tmp_path / "model.gguf")),
    )

    with pytest.raises(ProjectConfigError, match="must be relative"):
        invalid.validate(project)


def test_project_config_rejects_symlink(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    target = tmp_path / "external.toml"
    target.write_text("schema_version = 'bad'\n", encoding="utf-8")
    project.config_path.symlink_to(target)
    store = ProjectConfigStore(project, _default_config(project))

    with pytest.raises(ProjectConfigError, match="must be a regular file"):
        store.load()


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        (
            LocalModelSelection(artifact_id="", path=".heartwood/models/model"),
            "identifiers must not be empty",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                model_id="",
            ),
            "identifiers must not be empty",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                runtime="unsupported",
            ),
            "unsupported Heartwood-managed model runtime",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                catalog_source="unknown",
            ),
            "unsupported Heartwood-managed model catalog source",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                tier="unknown",
            ),
            "unsupported Heartwood-managed model tier",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                qualification="unknown",
            ),
            "unsupported Heartwood-managed model qualification",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                qualification="qualified",
            ),
            "qualified model qualification requires dated evidence",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                display_name=" ",
            ),
            "display_name must not be empty",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                source_repository="invalid",
            ),
            "source_repository must use owner/model format",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                source_revision="main",
            ),
            "source_revision must be immutable",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                catalog_source="user-selected",
                source_revision="abcdef0",
            ),
            "source_revision must be a resolved commit",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                source_path="../model.gguf",
            ),
            "source_path must be repository-relative",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                model_type="Not Normalized",
            ),
            "model_type must be normalized",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                size_bytes=0,
            ),
            "size_bytes must be positive",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                minimum_free_bytes=1,
            ),
            "minimum_free_bytes must cover its size",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                size_bytes=2,
                minimum_free_bytes=1,
            ),
            "minimum_free_bytes must cover its size",
        ),
        (
            LocalModelSelection(artifact_id="model", path=".heartwood/models"),
            r"under \.heartwood/models",
        ),
        (
            LocalModelSelection(artifact_id="model", path=".heartwood/cache/model"),
            r"under \.heartwood/models",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                context_window=1_048_577,
            ),
            "between 2048 and 1048576",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                maximum_context_window=2047,
            ),
            "maximum context window is invalid",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                minimum_gpu_count=-1,
            ),
            "GPU requirements cannot be negative",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                minimum_gpu_memory_bytes=-1,
            ),
            "GPU requirements cannot be negative",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                tensor_parallel_size=0,
            ),
            "tensor parallelism must be positive",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                startup_seconds_min=0,
            ),
            "startup estimate is invalid",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                startup_seconds_min=10,
                startup_seconds_max=9,
            ),
            "startup estimate is invalid",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                recommended_ram_bytes=0,
            ),
            "recommended_ram_bytes must be positive",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                recommended_disk_bytes=0,
            ),
            "recommended_disk_bytes must be positive",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                tool_call_parser="unknown",
            ),
            "unsupported Heartwood-managed tool-call parser",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                runtime="vllm",
                minimum_gpu_count=1,
                minimum_gpu_memory_bytes=1,
            ),
            "vLLM model runtime metadata is incomplete",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                runtime="llama-cpp",
                minimum_gpu_count=1,
            ),
            "llama.cpp models cannot declare vLLM GPU settings",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                license_id=" ",
            ),
            "license_id must not be empty",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                artifact_sha256="invalid",
            ),
            "artifact_sha256 must be a SHA-256 digest",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                catalog_source="user-selected",
            ),
            "provenance is incomplete",
        ),
        (
            LocalModelSelection(
                artifact_id="model",
                path=".heartwood/models/model",
                catalog_source="user-selected",
                display_name="Model",
                source_repository="example/model",
                source_revision="a" * 40,
                source_path="model.gguf",
                size_bytes=1,
                minimum_free_bytes=1,
                license_posture="Apache-2.0",
                minimum_resource_envelope="Minimum",
                recommended_resource_envelope="Recommended",
            ),
            "integrity metadata is incomplete",
        ),
    ],
)
def test_local_model_selection_rejects_invalid_metadata(
    tmp_path: Path,
    selection: LocalModelSelection,
    message: str,
) -> None:
    with pytest.raises(ProjectConfigError, match=message):
        selection.validate(ProjectContext(tmp_path))


def test_project_config_validates_schema_platform_and_source(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    config = _default_config(project)
    carina_policy = select_platform_adapter(
        {"HEARTWOOD_PLATFORM": "carina"}
    ).default_policy_profile()

    invalid = (
        (replace(config, schema_version="unknown"), "unsupported project configuration"),
        (replace(config, platform_id=""), "platform_id must not be empty"),
        (replace(config, policy=carina_policy), "policy platform does not match"),
        (replace(config, model_source="OpenAI"), "lowercase identifier"),
        (
            replace(config, action_settings=ActionSettings(schema_version="unknown")),
            "unsupported action settings schema",
        ),
    )

    for candidate, message in invalid:
        with pytest.raises(ProjectConfigError, match=message):
            candidate.validate(project)


def _static_connection(
    *,
    connection_id: str,
    source: Literal["platform", "user"],
) -> ModelConnection:
    return ModelConnection(
        connection_id=connection_id,
        label="Synthetic connection",
        protocol="static",
        model_prefix="openai/",
        source=source,
        credential_kind="environment",
        policy_endpoint="https://example.test/v1/chat/completions",
        catalog_endpoint=None,
        api_key_env="SYNTHETIC_API_KEY",
        static_models=("synthetic-model",),
    )


def test_project_config_rejects_non_platform_and_duplicate_connections(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    config = _default_config(project)

    with pytest.raises(ProjectConfigError, match="must be platform-provided"):
        replace(
            config,
            additional_connections=(_static_connection(connection_id="custom", source="user"),),
        ).validate(project)
    with pytest.raises(ProjectConfigError, match="ids must be unique"):
        replace(
            config,
            additional_connections=(_static_connection(connection_id="openai", source="platform"),),
        ).validate(project)


def test_project_config_parser_rejects_unsupported_structure(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    valid = _config_mapping(_default_config(project))

    with pytest.raises(ProjectConfigError, match="must be a table"):
        project_config_from_mapping([], project=project)
    with pytest.raises(ProjectConfigError, match="unsupported fields"):
        project_config_from_mapping({**valid, "secret": "value"}, project=project)
    with pytest.raises(ProjectConfigError, match="unsupported project configuration schema"):
        project_config_from_mapping({**valid, "schema_version": "unknown"}, project=project)
    with pytest.raises(ProjectConfigError, match="model_source must be a non-empty string"):
        project_config_from_mapping({**valid, "model_source": []}, project=project)
    with pytest.raises(ProjectConfigError, match="local_model must be a table"):
        project_config_from_mapping({**valid, "local_model": []}, project=project)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("unknown", "value", "local_model contains unsupported fields"),
        ("artifact_id", "", "artifact_id must be a non-empty string"),
        ("display_name", [], "display_name must be a non-empty string"),
        ("size_bytes", True, "size_bytes must be a positive integer"),
        ("minimum_gpu_count", True, "minimum_gpu_count must be a nonnegative integer"),
        ("allow_patterns", [""], "allow_patterns must be an array"),
    ],
)
def test_project_config_parser_rejects_invalid_local_model_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    project = ProjectContext(tmp_path)
    config = replace(
        _default_config(project),
        local_model=LocalModelSelection(
            artifact_id="model",
            path=".heartwood/models/model",
        ),
    )
    mapping = deepcopy(_config_mapping(config))
    local_model = mapping["local_model"]
    assert isinstance(local_model, dict)
    local_model[field] = value

    with pytest.raises(ProjectConfigError, match=message):
        project_config_from_mapping(mapping, project=project)


def test_project_config_parser_defaults_optional_local_model_collections(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    config = replace(
        _default_config(project),
        local_model=LocalModelSelection(
            artifact_id="model",
            path=".heartwood/models/model",
        ),
    )
    mapping = deepcopy(_config_mapping(config))
    local_model = mapping["local_model"]
    assert isinstance(local_model, dict)
    for field in (
        "minimum_gpu_count",
        "minimum_gpu_memory_bytes",
        "allow_patterns",
        "ignore_patterns",
        "validated_platforms",
        "catalog_source",
    ):
        local_model.pop(field, None)

    parsed = project_config_from_mapping(mapping, project=project)

    assert parsed.local_model is not None
    assert parsed.local_model.minimum_gpu_count == 0
    assert parsed.local_model.allow_patterns == ()
    assert parsed.local_model.catalog_source == "catalog"


def test_project_config_parser_rejects_non_platform_connection(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    mapping = _config_mapping(_default_config(project))
    connection = _static_connection(connection_id="custom", source="user")
    mapping["connections"] = [
        {
            **asdict(connection),
            "static_models": list(connection.static_models),
        }
    ]

    with pytest.raises(ProjectConfigError, match="must use source platform"):
        project_config_from_mapping(mapping, project=project)


def test_project_config_store_rejects_malformed_toml(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    project.config_path.write_text("[", encoding="utf-8")

    with pytest.raises(ProjectConfigError, match=r"unable to load \.heartwood/config.toml"):
        ProjectConfigStore(project, _default_config(project)).load()
