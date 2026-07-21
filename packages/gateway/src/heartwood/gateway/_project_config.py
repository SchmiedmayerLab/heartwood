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
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import tomli_w
from filelock import FileLock
from pydantic import ValidationError

from heartwood.gateway._action_settings import (
    ActionSettings,
    ActionSettingsError,
    action_settings_from_mapping,
)
from heartwood.gateway._local_model_contract import (
    DEFAULT_LOCAL_CONTEXT_WINDOW,
    MAXIMUM_LOCAL_CONTEXT_WINDOW,
    MINIMUM_LOCAL_CONTEXT_WINDOW,
)
from heartwood.gateway._model_catalog import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalogError,
    ModelConnection,
    model_connections_from_mapping,
)
from heartwood.gateway._model_identity import (
    is_hugging_face_model_id,
    is_immutable_revision,
    is_resolved_revision,
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
    model_id: str = "heartwood-managed-model"
    display_name: str | None = None
    source_repository: str | None = None
    source_revision: str | None = None
    source_path: str | None = None
    model_type: str | None = None
    size_bytes: int | None = None
    minimum_free_bytes: int | None = None
    license_posture: str | None = None
    license_id: str | None = None
    artifact_sha256: str | None = None
    context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW
    maximum_context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW
    minimum_resource_envelope: str | None = None
    recommended_resource_envelope: str | None = None
    precision: str | None = None
    tier: str = "standard"
    qualification: str = "candidate"
    minimum_gpu_count: int = 0
    minimum_gpu_memory_bytes: int = 0
    recommended_ram_bytes: int | None = None
    recommended_disk_bytes: int | None = None
    tool_call_parser: str | None = None
    tensor_parallel_size: int = 1
    startup_seconds_min: int = 30
    startup_seconds_max: int = 600
    download_policy: str | None = None
    allow_patterns: tuple[str, ...] = ()
    ignore_patterns: tuple[str, ...] = ()
    validated_platforms: tuple[str, ...] = ()
    qualification_test: str | None = None
    catalog_source: str = "catalog"

    def validate(self, project: ProjectContext) -> None:
        """Validate identifiers and keep the selected artifact under the model root."""
        if not self.artifact_id.strip() or not self.model_id.strip():
            raise ProjectConfigError("Heartwood-managed model identifiers must not be empty")
        if self.runtime not in {"auto", "llama-cpp", "vllm"}:
            raise ProjectConfigError(f"unsupported Heartwood-managed model runtime: {self.runtime}")
        if self.catalog_source not in {"catalog", "user-selected"}:
            raise ProjectConfigError("unsupported Heartwood-managed model catalog source")
        if self.tier not in {"standard", "powerful", "maximum"}:
            raise ProjectConfigError("unsupported Heartwood-managed model tier")
        if self.qualification not in {"candidate", "qualified"}:
            raise ProjectConfigError("unsupported Heartwood-managed model qualification")
        if self.display_name is not None and not self.display_name.strip():
            raise ProjectConfigError("Heartwood-managed model display_name must not be empty")
        if self.source_repository is not None and not is_hugging_face_model_id(
            self.source_repository
        ):
            raise ProjectConfigError(
                "Heartwood-managed model source_repository must use owner/model format"
            )
        if self.source_revision is not None and not is_immutable_revision(self.source_revision):
            raise ProjectConfigError("Heartwood-managed model source_revision must be immutable")
        if (
            self.catalog_source == "user-selected"
            and self.source_revision is not None
            and not is_resolved_revision(self.source_revision)
        ):
            raise ProjectConfigError(
                "user-selected Heartwood-managed model source_revision must be a resolved commit"
            )
        if self.source_path is not None:
            source_path = Path(self.source_path)
            if source_path.is_absolute() or ".." in source_path.parts:
                raise ProjectConfigError(
                    "Heartwood-managed model source_path must be repository-relative"
                )
        if self.model_type is not None and _SAFE_SOURCE.fullmatch(self.model_type) is None:
            raise ProjectConfigError("Heartwood-managed model_type must be normalized")
        if self.size_bytes is not None and self.size_bytes <= 0:
            raise ProjectConfigError("Heartwood-managed model size_bytes must be positive")
        if self.minimum_free_bytes is not None and (
            self.size_bytes is None or self.minimum_free_bytes < self.size_bytes
        ):
            raise ProjectConfigError(
                "Heartwood-managed model minimum_free_bytes must cover its size"
            )
        if not MINIMUM_LOCAL_CONTEXT_WINDOW <= self.context_window <= MAXIMUM_LOCAL_CONTEXT_WINDOW:
            raise ProjectConfigError(
                f"Heartwood-managed model context_window must be between 2048 and "
                f"{MAXIMUM_LOCAL_CONTEXT_WINDOW} tokens"
            )
        if not self.context_window <= self.maximum_context_window <= MAXIMUM_LOCAL_CONTEXT_WINDOW:
            raise ProjectConfigError("Heartwood-managed maximum context window is invalid")
        if self.minimum_gpu_count < 0 or self.minimum_gpu_memory_bytes < 0:
            raise ProjectConfigError("Heartwood-managed GPU requirements cannot be negative")
        if self.tensor_parallel_size < 1:
            raise ProjectConfigError("Heartwood-managed tensor parallelism must be positive")
        if self.startup_seconds_min <= 0 or self.startup_seconds_max < self.startup_seconds_min:
            raise ProjectConfigError("Heartwood-managed startup estimate is invalid")
        for field_name, value in (
            ("recommended_ram_bytes", self.recommended_ram_bytes),
            ("recommended_disk_bytes", self.recommended_disk_bytes),
        ):
            if value is not None and value <= 0:
                raise ProjectConfigError(f"Heartwood-managed {field_name} must be positive")
        if self.tool_call_parser is not None and self.tool_call_parser not in {
            "hermes",
            "openai",
            "qwen3_coder",
        }:
            raise ProjectConfigError("unsupported Heartwood-managed tool-call parser")
        if self.runtime == "vllm" and (
            self.minimum_gpu_count < 1
            or self.minimum_gpu_memory_bytes < 1
            or self.tool_call_parser is None
        ):
            raise ProjectConfigError("vLLM model runtime metadata is incomplete")
        if self.runtime == "llama-cpp" and (
            self.minimum_gpu_count != 0
            or self.minimum_gpu_memory_bytes != 0
            or self.tool_call_parser is not None
        ):
            raise ProjectConfigError("llama.cpp models cannot declare vLLM GPU settings")
        for metadata_name, metadata_value in (
            ("license_posture", self.license_posture),
            ("license_id", self.license_id),
            ("precision", self.precision),
            ("download_policy", self.download_policy),
            ("qualification_test", self.qualification_test),
            ("minimum_resource_envelope", self.minimum_resource_envelope),
            ("recommended_resource_envelope", self.recommended_resource_envelope),
        ):
            if metadata_value is not None and not metadata_value.strip():
                raise ProjectConfigError(
                    f"Heartwood-managed model {metadata_name} must not be empty"
                )
        if (
            self.artifact_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256) is None
        ):
            raise ProjectConfigError(
                "Heartwood-managed model artifact_sha256 must be a SHA-256 digest"
            )
        if self.catalog_source == "user-selected" and any(
            value is None
            for value in (
                self.display_name,
                self.source_repository,
                self.source_revision,
                self.size_bytes,
                self.minimum_free_bytes,
                self.license_posture,
                self.minimum_resource_envelope,
                self.recommended_resource_envelope,
            )
        ):
            raise ProjectConfigError(
                "user-selected Heartwood-managed model provenance is incomplete"
            )
        if (
            self.catalog_source == "user-selected"
            and self.source_path is not None
            and self.artifact_sha256 is None
        ):
            raise ProjectConfigError("user-selected GGUF model integrity metadata is incomplete")
        configured = Path(self.path)
        if configured.is_absolute():
            raise ProjectConfigError("Heartwood-managed model path must be relative to the project")
        resolved = (project.root / configured).resolve()
        if project.models_dir not in resolved.parents:
            raise ProjectConfigError(
                "Heartwood-managed model path must remain under .heartwood/models"
            )

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
        with FileLock(self.project.config_lock_path, mode=0o600):
            self._save_unlocked(config)

    def update(self, transform: Callable[[ProjectConfig], ProjectConfig]) -> ProjectConfig:
        """Apply one read-modify-write operation under the project configuration lock."""
        self.project.initialize()
        with FileLock(self.project.config_lock_path, mode=0o600):
            current = self.load()
            updated = transform(current)
            updated.validate(self.project)
            if updated == current:
                return current
            self._save_unlocked(updated)
            return updated

    def restore(self, config: ProjectConfig | None) -> None:
        """Restore a configuration snapshot after a larger transaction fails."""
        self.project.initialize()
        with FileLock(self.project.config_lock_path, mode=0o600):
            if config is None:
                self.project.config_path.unlink(missing_ok=True)
            else:
                self._save_unlocked(config)

    def _save_unlocked(self, config: ProjectConfig) -> None:
        config.validate(self.project)
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
        model_id: str = "heartwood-managed-model",
        display_name: str | None = None,
        source_repository: str | None = None,
        source_revision: str | None = None,
        source_path: str | None = None,
        model_type: str | None = None,
        size_bytes: int | None = None,
        minimum_free_bytes: int | None = None,
        license_posture: str | None = None,
        license_id: str | None = None,
        artifact_sha256: str | None = None,
        context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW,
        maximum_context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW,
        minimum_resource_envelope: str | None = None,
        recommended_resource_envelope: str | None = None,
        precision: str | None = None,
        tier: str = "standard",
        qualification: str = "candidate",
        minimum_gpu_count: int = 0,
        minimum_gpu_memory_bytes: int = 0,
        recommended_ram_bytes: int | None = None,
        recommended_disk_bytes: int | None = None,
        tool_call_parser: str | None = None,
        tensor_parallel_size: int = 1,
        startup_seconds_min: int = 30,
        startup_seconds_max: int = 600,
        download_policy: str | None = None,
        allow_patterns: tuple[str, ...] = (),
        ignore_patterns: tuple[str, ...] = (),
        validated_platforms: tuple[str, ...] = (),
        qualification_test: str | None = None,
        catalog_source: str = "catalog",
        settings: ModelSettings | None = None,
    ) -> ProjectConfig:
        """Persist one verified Heartwood-managed model and optional active profile."""
        resolved = path.resolve()
        if self.project.models_dir not in resolved.parents:
            raise ProjectConfigError("downloaded model must be stored under .heartwood/models")
        selection = LocalModelSelection(
            artifact_id=artifact_id,
            path=str(resolved.relative_to(self.project.root)),
            runtime=runtime,
            model_id=model_id,
            display_name=display_name,
            source_repository=source_repository,
            source_revision=source_revision,
            source_path=source_path,
            model_type=model_type,
            size_bytes=size_bytes,
            minimum_free_bytes=minimum_free_bytes,
            license_posture=license_posture,
            license_id=license_id,
            artifact_sha256=artifact_sha256,
            context_window=context_window,
            maximum_context_window=maximum_context_window,
            minimum_resource_envelope=minimum_resource_envelope,
            recommended_resource_envelope=recommended_resource_envelope,
            precision=precision,
            tier=tier,
            qualification=qualification,
            minimum_gpu_count=minimum_gpu_count,
            minimum_gpu_memory_bytes=minimum_gpu_memory_bytes,
            recommended_ram_bytes=recommended_ram_bytes,
            recommended_disk_bytes=recommended_disk_bytes,
            tool_call_parser=tool_call_parser,
            tensor_parallel_size=tensor_parallel_size,
            startup_seconds_min=startup_seconds_min,
            startup_seconds_max=startup_seconds_max,
            download_policy=download_policy,
            allow_patterns=allow_patterns,
            ignore_patterns=ignore_patterns,
            validated_platforms=validated_platforms,
            qualification_test=qualification_test,
            catalog_source=catalog_source,
        )

        def apply(current: ProjectConfig) -> ProjectConfig:
            updated = replace(current, model_source="heartwood", local_model=selection)
            return updated if settings is None else updated.with_model_settings(settings)

        return self.update(apply)

    def select_model_source(
        self,
        source: str | None,
        settings: ModelSettings,
    ) -> ProjectConfig:
        """Persist model settings and their canonical source in one atomic write."""
        return self.update(lambda current: current.with_model_selection(source, settings))


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
            self.store.update(lambda current: current.with_model_settings(settings))
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
            self.store.update(lambda current: current.with_action_settings(settings))
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
        result["local_model"] = _without_none(asdict(config.local_model))
    return result


def _local_model_from_mapping(value: object) -> LocalModelSelection:
    if not isinstance(value, dict):
        raise ProjectConfigError("local_model must be a table")
    unknown = sorted(
        set(value)
        - {
            "artifact_id",
            "artifact_sha256",
            "display_name",
            "download_policy",
            "allow_patterns",
            "ignore_patterns",
            "license_posture",
            "license_id",
            "minimum_free_bytes",
            "minimum_gpu_count",
            "minimum_gpu_memory_bytes",
            "minimum_resource_envelope",
            "model_id",
            "model_type",
            "maximum_context_window",
            "path",
            "precision",
            "qualification",
            "qualification_test",
            "recommended_disk_bytes",
            "recommended_ram_bytes",
            "recommended_resource_envelope",
            "catalog_source",
            "context_window",
            "runtime",
            "size_bytes",
            "source_path",
            "source_repository",
            "source_revision",
            "startup_seconds_max",
            "startup_seconds_min",
            "tensor_parallel_size",
            "tier",
            "tool_call_parser",
            "validated_platforms",
        }
    )
    if unknown:
        raise ProjectConfigError(f"local_model contains unsupported fields: {', '.join(unknown)}")
    context_window = (
        _optional_positive_int(value.get("context_window"), "context_window")
        or DEFAULT_LOCAL_CONTEXT_WINDOW
    )
    return LocalModelSelection(
        artifact_id=_required_string(value, "artifact_id"),
        path=_required_string(value, "path"),
        runtime=_required_string(value, "runtime"),
        model_id=_required_string(value, "model_id"),
        display_name=_optional_string(value.get("display_name"), "display_name"),
        source_repository=_optional_string(value.get("source_repository"), "source_repository"),
        source_revision=_optional_string(value.get("source_revision"), "source_revision"),
        source_path=_optional_string(value.get("source_path"), "source_path"),
        model_type=_optional_string(value.get("model_type"), "model_type"),
        size_bytes=_optional_positive_int(value.get("size_bytes"), "size_bytes"),
        minimum_free_bytes=_optional_positive_int(
            value.get("minimum_free_bytes"), "minimum_free_bytes"
        ),
        license_posture=_optional_string(value.get("license_posture"), "license_posture"),
        license_id=_optional_string(value.get("license_id"), "license_id"),
        artifact_sha256=_optional_string(value.get("artifact_sha256"), "artifact_sha256"),
        context_window=context_window,
        maximum_context_window=_optional_positive_int(
            value.get("maximum_context_window"), "maximum_context_window"
        )
        or context_window,
        minimum_resource_envelope=_optional_string(
            value.get("minimum_resource_envelope"), "minimum_resource_envelope"
        ),
        recommended_resource_envelope=_optional_string(
            value.get("recommended_resource_envelope"), "recommended_resource_envelope"
        ),
        precision=_optional_string(value.get("precision"), "precision"),
        tier=_optional_string(value.get("tier"), "tier") or "standard",
        qualification=_optional_string(value.get("qualification"), "qualification") or "candidate",
        minimum_gpu_count=_optional_nonnegative_int(
            value.get("minimum_gpu_count"), "minimum_gpu_count"
        ),
        minimum_gpu_memory_bytes=_optional_nonnegative_int(
            value.get("minimum_gpu_memory_bytes"), "minimum_gpu_memory_bytes"
        ),
        recommended_ram_bytes=_optional_positive_int(
            value.get("recommended_ram_bytes"), "recommended_ram_bytes"
        ),
        recommended_disk_bytes=_optional_positive_int(
            value.get("recommended_disk_bytes"), "recommended_disk_bytes"
        ),
        tool_call_parser=_optional_string(value.get("tool_call_parser"), "tool_call_parser"),
        tensor_parallel_size=_optional_positive_int(
            value.get("tensor_parallel_size"), "tensor_parallel_size"
        )
        or 1,
        startup_seconds_min=_optional_positive_int(
            value.get("startup_seconds_min"), "startup_seconds_min"
        )
        or 30,
        startup_seconds_max=_optional_positive_int(
            value.get("startup_seconds_max"), "startup_seconds_max"
        )
        or 600,
        download_policy=_optional_string(value.get("download_policy"), "download_policy"),
        allow_patterns=_optional_string_tuple(value.get("allow_patterns"), "allow_patterns"),
        ignore_patterns=_optional_string_tuple(value.get("ignore_patterns"), "ignore_patterns"),
        validated_platforms=_optional_string_tuple(
            value.get("validated_platforms"), "validated_platforms"
        ),
        qualification_test=_optional_string(value.get("qualification_test"), "qualification_test"),
        catalog_source=_optional_string(value.get("catalog_source"), "catalog_source")
        or "recommended",
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


def _optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProjectConfigError(f"{name} must be a positive integer when provided")
    return value


def _optional_nonnegative_int(value: object, name: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectConfigError(f"{name} must be a nonnegative integer when provided")
    return value


def _optional_string_tuple(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ProjectConfigError(f"{name} must be an array of non-empty strings")
    return tuple(value)
