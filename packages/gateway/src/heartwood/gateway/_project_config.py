# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Typed project configuration stored in ``.heartwood/config.toml``."""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from heartwood.gateway._action_settings import (
    ActionSettings,
    ActionSettingsError,
    action_settings_from_mapping,
)
from heartwood.gateway._model_catalog import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalogError,
    ModelConnection,
    model_connections_from_mapping,
)
from heartwood.gateway._model_settings import (
    ModelSettings,
    ModelSettingsError,
    model_settings_from_mapping,
)
from heartwood.gateway._project import ProjectContext
from heartwood.schemas import PolicyProfile

_CONFIG_SCHEMA_VERSION = "heartwood.project-config.v1"
_SAFE_SOURCE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TOP_LEVEL_FIELDS = {
    "action",
    "connections",
    "local_model",
    "models",
    "model_source",
    "platform_id",
    "policy",
    "schema_version",
}


class ProjectConfigError(ValueError):
    """Raised when project configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class LocalModelSelection:
    """One downloaded local artifact selected for managed runtime launch."""

    artifact_id: str
    path: str
    runtime: str = "auto"
    model_id: str = "heartwood-local-model"

    def validate(self, project: ProjectContext) -> None:
        """Validate identifiers and keep the selected artifact under the model root."""
        if not self.artifact_id.strip() or not self.model_id.strip():
            raise ProjectConfigError("local model identifiers must not be empty")
        if self.runtime not in {"auto", "llama-cpp", "vllm"}:
            raise ProjectConfigError(f"unsupported local model runtime: {self.runtime}")
        configured = Path(self.path)
        if configured.is_absolute():
            raise ProjectConfigError("local model path must be relative to the project")
        resolved = (project.root / configured).resolve()
        if project.models_dir not in resolved.parents:
            raise ProjectConfigError("local model path must remain under .heartwood/models")

    def resolved_path(self, project: ProjectContext) -> Path:
        """Return the validated absolute artifact path."""
        self.validate(project)
        return (project.root / self.path).resolve()


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """The complete non-secret configuration for one project."""

    platform_id: str
    policy: PolicyProfile
    model_source: str | None = None
    action_settings: ActionSettings = field(default_factory=ActionSettings)
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    additional_connections: tuple[ModelConnection, ...] = ()
    local_model: LocalModelSelection | None = None
    schema_version: str = _CONFIG_SCHEMA_VERSION

    def validate(self, project: ProjectContext) -> None:
        """Validate the complete configuration and its project-bound paths."""
        if self.schema_version != _CONFIG_SCHEMA_VERSION:
            raise ProjectConfigError(f"unsupported project configuration: {self.schema_version}")
        if not self.platform_id.strip():
            raise ProjectConfigError("platform_id must not be empty")
        if self.policy.platform_id != self.platform_id:
            raise ProjectConfigError("project policy platform does not match platform_id")
        if self.model_source is not None and _SAFE_SOURCE.fullmatch(self.model_source) is None:
            raise ProjectConfigError("model_source must be a lowercase identifier")
        try:
            self.action_settings.validate()
            self.model_settings.validate()
            for connection in self.additional_connections:
                connection.validate()
        except (ActionSettingsError, ModelCatalogError, ModelSettingsError) as error:
            raise ProjectConfigError(str(error)) from error
        if any(connection.source != "platform" for connection in self.additional_connections):
            raise ProjectConfigError("project connections must be platform-provided")
        connection_ids = [
            connection.connection_id
            for connection in (*BUILT_IN_MODEL_CONNECTIONS, *self.additional_connections)
        ]
        if len(connection_ids) != len(set(connection_ids)):
            raise ProjectConfigError("model connection ids must be unique")
        if self.local_model is not None:
            self.local_model.validate(project)

    def with_model_settings(self, settings: ModelSettings) -> ProjectConfig:
        """Return configuration with a validated model-settings replacement."""
        settings.validate()
        return replace(self, model_settings=settings)

    def with_action_settings(self, settings: ActionSettings) -> ProjectConfig:
        """Return configuration with a validated action-settings replacement."""
        settings.validate()
        return replace(self, action_settings=settings)

    def with_model_selection(
        self,
        source: str | None,
        settings: ModelSettings,
    ) -> ProjectConfig:
        """Return configuration with one active model source and settings value."""
        settings.validate()
        return replace(self, model_source=source, model_settings=settings)


class ProjectConfigStore:
    """Atomically load and persist one project configuration."""

    def __init__(self, project: ProjectContext, default: ProjectConfig) -> None:
        self.project = project
        self.default = default
        self.default.validate(project)

    @property
    def configured(self) -> bool:
        """Return whether the project has a persisted configuration."""
        return self.project.config_path.is_file() and not self.project.config_path.is_symlink()

    def load(self) -> ProjectConfig:
        """Load configuration or return the unsaved platform default."""
        if not self.project.config_path.exists():
            return self.default
        if self.project.config_path.is_symlink() or not self.project.config_path.is_file():
            raise ProjectConfigError(".heartwood/config.toml must be a regular file")
        try:
            with self.project.config_path.open("rb") as file:
                value = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ProjectConfigError(f"unable to load .heartwood/config.toml: {error}") from error
        config = project_config_from_mapping(value, project=self.project)
        config.validate(self.project)
        return config

    def save(self, config: ProjectConfig) -> None:
        """Persist validated configuration as owner-only TOML."""
        config.validate(self.project)
        self.project.initialize()
        payload = _config_mapping(config)
        contents = tomli_w.dumps(payload)
        try:
            tomllib.loads(contents)
        except tomllib.TOMLDecodeError as error:  # pragma: no cover - writer invariant
            raise ProjectConfigError("generated project configuration is invalid TOML") from error
        descriptor, temporary = tempfile.mkstemp(
            prefix=".config.toml.", dir=self.project.state_root
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(contents)
            temporary_path.chmod(0o600)
            temporary_path.replace(self.project.config_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def select_local_model(
        self,
        *,
        artifact_id: str,
        path: Path,
        runtime: str = "auto",
        model_id: str = "heartwood-local-model",
        settings: ModelSettings | None = None,
    ) -> ProjectConfig:
        """Persist one verified project-local model and optional active profile."""
        resolved = path.resolve()
        if self.project.models_dir not in resolved.parents:
            raise ProjectConfigError("downloaded model must be stored under .heartwood/models")
        selection = LocalModelSelection(
            artifact_id=artifact_id,
            path=str(resolved.relative_to(self.project.root)),
            runtime=runtime,
            model_id=model_id,
        )
        updated = replace(self.load(), model_source="local", local_model=selection)
        if settings is not None:
            updated = updated.with_model_settings(settings)
        self.save(updated)
        return updated

    def select_model_source(
        self,
        source: str | None,
        settings: ModelSettings,
    ) -> ProjectConfig:
        """Persist model settings and their canonical source in one atomic write."""
        updated = self.load().with_model_selection(source, settings)
        self.save(updated)
        return updated


class ProjectModelSettingsStore:
    """Expose the model-settings section through the existing store contract."""

    def __init__(self, store: ProjectConfigStore) -> None:
        self.store = store

    def load(self) -> ModelSettings:
        """Load project model settings."""
        try:
            return self.store.load().model_settings
        except ProjectConfigError as error:
            raise ModelSettingsError(str(error)) from error

    def save(self, settings: ModelSettings) -> None:
        """Replace project model settings atomically."""
        try:
            self.store.save(self.store.load().with_model_settings(settings))
        except ProjectConfigError as error:
            raise ModelSettingsError(str(error)) from error

    def save_selection(self, source: str | None, settings: ModelSettings) -> None:
        """Persist a selected model and its source atomically."""
        try:
            self.store.select_model_source(source, settings)
        except ProjectConfigError as error:
            raise ModelSettingsError(str(error)) from error


class ProjectActionSettingsStore:
    """Expose the action-settings section through the existing store contract."""

    def __init__(self, store: ProjectConfigStore) -> None:
        self.store = store

    def load(self) -> ActionSettings:
        """Load project action settings."""
        try:
            return self.store.load().action_settings
        except ProjectConfigError as error:
            raise ActionSettingsError(str(error)) from error

    def save(self, settings: ActionSettings) -> None:
        """Replace project action settings atomically."""
        try:
            self.store.save(self.store.load().with_action_settings(settings))
        except ProjectConfigError as error:
            raise ActionSettingsError(str(error)) from error


def project_config_from_mapping(value: object, *, project: ProjectContext) -> ProjectConfig:
    """Validate one TOML-decoded project configuration."""
    if not isinstance(value, dict):
        raise ProjectConfigError("project configuration must be a table")
    unknown = sorted(set(value) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise ProjectConfigError(
            f"project configuration contains unsupported fields: {', '.join(unknown)}"
        )
    if value.get("schema_version") != _CONFIG_SCHEMA_VERSION:
        raise ProjectConfigError("unsupported project configuration schema")
    platform_id = _required_string(value, "platform_id")
    model_source = _optional_string(value.get("model_source"), "model_source")
    try:
        action_settings = action_settings_from_mapping(value.get("action"))
        model_settings = model_settings_from_mapping(value.get("models"))
        policy = PolicyProfile.model_validate(value.get("policy"))
        connections = model_connections_from_mapping(
            {
                "schema_version": "heartwood.model-connections.v1",
                "connections": value.get("connections", []),
            }
        )
    except (ActionSettingsError, ModelCatalogError, ModelSettingsError, ValidationError) as error:
        raise ProjectConfigError(str(error)) from error
    additional = tuple(connection for connection in connections if connection.source == "platform")
    local_value = value.get("local_model")
    local_model = None if local_value is None else _local_model_from_mapping(local_value)
    config = ProjectConfig(
        schema_version=_CONFIG_SCHEMA_VERSION,
        platform_id=platform_id,
        model_source=model_source,
        action_settings=action_settings,
        model_settings=model_settings,
        additional_connections=additional,
        policy=policy,
        local_model=local_model,
    )
    config.validate(project)
    return config


def _config_mapping(config: ProjectConfig) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": config.schema_version,
        "platform_id": config.platform_id,
        "action": config.action_settings.safe_dict(),
        "models": {
            "schema_version": config.model_settings.schema_version,
            "profiles": [
                _without_none(profile.safe_dict()) for profile in config.model_settings.profiles
            ],
        },
        "connections": [
            _without_none(asdict(connection)) for connection in config.additional_connections
        ],
        "policy": config.policy.model_dump(mode="python", exclude_none=True),
    }
    if config.model_source is not None:
        result["model_source"] = config.model_source
    if config.model_settings.active_profile is not None:
        models = result["models"]
        if not isinstance(models, dict):  # pragma: no cover - local invariant
            raise ProjectConfigError("invalid generated model configuration")
        models["active_profile"] = config.model_settings.active_profile
    if config.local_model is not None:
        result["local_model"] = asdict(config.local_model)
    return result


def _local_model_from_mapping(value: object) -> LocalModelSelection:
    if not isinstance(value, dict):
        raise ProjectConfigError("local_model must be a table")
    unknown = sorted(set(value) - {"artifact_id", "model_id", "path", "runtime"})
    if unknown:
        raise ProjectConfigError(f"local_model contains unsupported fields: {', '.join(unknown)}")
    return LocalModelSelection(
        artifact_id=_required_string(value, "artifact_id"),
        path=_required_string(value, "path"),
        runtime=_required_string(value, "runtime"),
        model_id=_required_string(value, "model_id"),
    )


def _without_none(value: Mapping[str, Any]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item is not None}


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ProjectConfigError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(f"{name} must be a non-empty string when provided")
    return value
