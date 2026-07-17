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
from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._jupyter import has_authenticated_jupyter_proxy, jupyter_proxy_url
from heartwood.gateway._local_model_contract import (
    LocalContextPlan,
    estimate_local_runtime_memory,
    plan_local_context_window,
)
from heartwood.gateway._local_models import (
    HuggingFaceModelRepository,
    LocalModelChoice,
    LocalModelDownloadPlan,
    LocalModelRuntime,
    ModelRepositoryError,
    ModelRepositoryInspection,
    recommended_model_choices,
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
    ModelSnapshot,
    ModelSnapshotCatalog,
    ModelSnapshotError,
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
    persist_deployment_profile,
)
from heartwood.gateway._rest import RestGateway, RestRequest, RestResponse
from heartwood.gateway._session_catalog import (
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
from heartwood.gateway._stream import GatewayEventStream

__all__ = [
    "ACTION_MODE_OPTIONS",
    "BUILT_IN_MODEL_CONNECTIONS",
    "MODEL_PRESETS",
    "MODEL_SOURCE_OPTIONS",
    "ActionModeOption",
    "ActionSettings",
    "ActionSettingsError",
    "DeploymentReadiness",
    "GatewayAsgiApp",
    "GatewayEventStream",
    "HuggingFaceModelRepository",
    "LocalContextPlan",
    "LocalModelChoice",
    "LocalModelDownloadManager",
    "LocalModelDownloadPlan",
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
    "ModelRepositoryError",
    "ModelRepositoryInspection",
    "ModelSettings",
    "ModelSettingsError",
    "ModelSnapshot",
    "ModelSnapshotCatalog",
    "ModelSnapshotError",
    "ModelSourceOption",
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
    "SkillManager",
    "SkillSettingsError",
    "SkillSummary",
    "action_settings_from_mapping",
    "custom_model_connection",
    "download_model_artifact",
    "download_model_snapshot",
    "estimate_local_runtime_memory",
    "has_authenticated_jupyter_proxy",
    "inspect_deployment",
    "jupyter_proxy_url",
    "load_model_artifact_catalog",
    "load_model_connections",
    "load_model_snapshot_catalog",
    "model_connections_from_mapping",
    "model_profile_from_mapping",
    "model_profile_from_preset",
    "model_settings_from_mapping",
    "persist_deployment_profile",
    "plan_local_context_window",
    "recommended_model_choices",
    "verify_model_artifact",
    "verify_model_snapshot",
]
