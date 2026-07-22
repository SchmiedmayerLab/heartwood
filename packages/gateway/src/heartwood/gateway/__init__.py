# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session gateway, OpenHands adapter, and non-secret model settings."""

from __future__ import annotations

from heartwood.gateway._action_settings import (
    ACTION_MODE_OPTIONS,
    ActionModeOption,
    ActionSettings,
    ActionSettingsError,
    action_settings_from_mapping,
)
from heartwood.gateway._asgi import GatewayAsgiApp
from heartwood.gateway._credentials import (
    CredentialBindingStatus,
    CredentialStore,
    CredentialStoreAvailability,
    CredentialStoreError,
)
from heartwood.gateway._diagnostics import (
    DiagnosticDefinition,
    diagnostic_catalog,
    diagnostic_for,
)
from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._gpu_environment import (
    GpuCapacity,
    GpuDevice,
    GpuEnvironment,
    SlurmGpuPartition,
    discover_slurm_gpu_partitions,
    discover_visible_gpus,
    inspect_gpu_environment,
    minimum_compute_capability_for_model,
)
from heartwood.gateway._local_import import LocalModelImport, import_local_model
from heartwood.gateway._local_model_contract import (
    LocalContextPlan,
    estimate_local_runtime_memory,
    managed_model_native_tool_calling,
    managed_model_request_body,
    managed_model_token_budgets,
    plan_local_context_window,
)
from heartwood.gateway._local_models import (
    HuggingFaceModelRepository,
    LocalModelChoice,
    LocalModelDownloadPlan,
    LocalModelRuntime,
    ModelRepositoryError,
    ModelRepositoryInspection,
    catalog_model_choices,
)
from heartwood.gateway._model_artifacts import (
    LocalModelDownloadManager,
    ModelArtifact,
    ModelArtifactCatalog,
    ModelArtifactError,
    ModelDownload,
    download_model_artifact,
    load_model_artifact_catalog,
    verify_model_artifact,
)
from heartwood.gateway._model_catalog import (
    BUILT_IN_MODEL_CONNECTIONS,
    ModelCatalog,
    ModelCatalogEntry,
    ModelCatalogError,
    ModelCatalogService,
    ModelConnection,
    ProviderModel,
    custom_model_connection,
    custom_model_connection_requires_token,
    load_model_connections,
    model_connections_from_mapping,
)
from heartwood.gateway._model_settings import (
    MODEL_PRESETS,
    ModelPreset,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    model_profile_from_mapping,
    model_profile_from_preset,
    model_settings_from_mapping,
)
from heartwood.gateway._model_snapshots import (
    ModelQualification,
    ModelSnapshot,
    ModelSnapshotCatalog,
    ModelSnapshotError,
    ModelTier,
    ToolCallParser,
    automatic_model_tier,
    download_model_snapshot,
    load_model_snapshot_catalog,
    verify_model_snapshot,
)
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend, OpenHandsSdkError
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._project_config import (
    LocalModelSelection,
    ProjectConfig,
    ProjectConfigError,
    ProjectConfigStore,
)
from heartwood.gateway._readiness import (
    MODEL_SOURCE_OPTIONS,
    DeploymentReadiness,
    ModelSourceOption,
    ReadinessCheck,
    inspect_deployment,
    model_source_options,
    persist_deployment_profile,
)
from heartwood.gateway._rest import RestGateway, RestRequest, RestResponse
from heartwood.gateway._session_catalog import (
    DEFAULT_SESSION_ID,
    SessionCatalog,
    SessionCatalogError,
    SessionNotFoundError,
    SessionSummary,
)
from heartwood.gateway._skill_settings import (
    SkillManager,
    SkillSettingsError,
    SkillSummary,
)
from heartwood.gateway._startup import InterfaceKind, SetupPhase, StartupPlan, plan_startup
from heartwood.gateway._stream import GatewayEventStream

__all__ = [
    "ACTION_MODE_OPTIONS",
    "BUILT_IN_MODEL_CONNECTIONS",
    "DEFAULT_SESSION_ID",
    "MODEL_PRESETS",
    "MODEL_SOURCE_OPTIONS",
    "ActionModeOption",
    "ActionSettings",
    "ActionSettingsError",
    "CredentialBindingStatus",
    "CredentialStore",
    "CredentialStoreAvailability",
    "CredentialStoreError",
    "DeploymentReadiness",
    "DiagnosticDefinition",
    "GatewayAsgiApp",
    "GatewayEventStream",
    "GpuCapacity",
    "GpuDevice",
    "GpuEnvironment",
    "HuggingFaceModelRepository",
    "InterfaceKind",
    "LocalContextPlan",
    "LocalModelChoice",
    "LocalModelDownloadManager",
    "LocalModelDownloadPlan",
    "LocalModelImport",
    "LocalModelRuntime",
    "LocalModelSelection",
    "ModelArtifact",
    "ModelArtifactCatalog",
    "ModelArtifactError",
    "ModelCatalog",
    "ModelCatalogEntry",
    "ModelCatalogError",
    "ModelCatalogService",
    "ModelConnection",
    "ModelDownload",
    "ModelPreset",
    "ModelProfile",
    "ModelQualification",
    "ModelRepositoryError",
    "ModelRepositoryInspection",
    "ModelSettings",
    "ModelSettingsError",
    "ModelSnapshot",
    "ModelSnapshotCatalog",
    "ModelSnapshotError",
    "ModelSourceOption",
    "ModelTier",
    "OpenHandsSdkBackend",
    "OpenHandsSdkError",
    "ProjectConfig",
    "ProjectConfigError",
    "ProjectConfigStore",
    "ProjectContext",
    "ProjectStateError",
    "ProviderModel",
    "ReadinessCheck",
    "RestGateway",
    "RestRequest",
    "RestResponse",
    "SessionCatalog",
    "SessionCatalogError",
    "SessionGateway",
    "SessionNotFoundError",
    "SessionSummary",
    "SetupPhase",
    "SkillManager",
    "SkillSettingsError",
    "SkillSummary",
    "SlurmGpuPartition",
    "StartupPlan",
    "ToolCallParser",
    "action_settings_from_mapping",
    "automatic_model_tier",
    "catalog_model_choices",
    "custom_model_connection",
    "custom_model_connection_requires_token",
    "diagnostic_catalog",
    "diagnostic_for",
    "discover_slurm_gpu_partitions",
    "discover_visible_gpus",
    "download_model_artifact",
    "download_model_snapshot",
    "estimate_local_runtime_memory",
    "import_local_model",
    "inspect_deployment",
    "inspect_gpu_environment",
    "load_model_artifact_catalog",
    "load_model_connections",
    "load_model_snapshot_catalog",
    "managed_model_native_tool_calling",
    "managed_model_request_body",
    "managed_model_token_budgets",
    "minimum_compute_capability_for_model",
    "model_connections_from_mapping",
    "model_profile_from_mapping",
    "model_profile_from_preset",
    "model_settings_from_mapping",
    "model_source_options",
    "persist_deployment_profile",
    "plan_local_context_window",
    "plan_startup",
    "verify_model_artifact",
    "verify_model_snapshot",
]
