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
    ActionSettingsStore,
    action_settings_from_mapping,
    action_settings_path,
)
from heartwood.gateway._asgi import GatewayAsgiApp
from heartwood.gateway._gateway import SessionGateway
from heartwood.gateway._model_artifacts import (
    ModelArtifact,
    ModelArtifactCatalog,
    ModelArtifactError,
    ModelArtifactManager,
    ModelDownload,
    download_model_artifact,
    load_model_artifact_catalog,
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
    model_connections_path,
)
from heartwood.gateway._model_settings import (
    MODEL_PRESETS,
    ModelPreset,
    ModelProfile,
    ModelSettings,
    ModelSettingsError,
    ModelSettingsStore,
    model_profile_from_mapping,
    model_profile_from_preset,
    model_settings_from_mapping,
    model_settings_path,
)
from heartwood.gateway._openhands_sdk import OpenHandsSdkBackend, OpenHandsSdkError
from heartwood.gateway._readiness import (
    DeploymentReadiness,
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
    "ActionModeOption",
    "ActionSettings",
    "ActionSettingsError",
    "ActionSettingsStore",
    "DeploymentReadiness",
    "GatewayAsgiApp",
    "GatewayEventStream",
    "ModelArtifact",
    "ModelArtifactCatalog",
    "ModelArtifactError",
    "ModelArtifactManager",
    "ModelCatalog",
    "ModelCatalogEntry",
    "ModelCatalogError",
    "ModelCatalogService",
    "ModelConnection",
    "ModelDownload",
    "ModelPreset",
    "ModelProfile",
    "ModelSettings",
    "ModelSettingsError",
    "ModelSettingsStore",
    "OpenHandsSdkBackend",
    "OpenHandsSdkError",
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
    "action_settings_path",
    "custom_model_connection",
    "download_model_artifact",
    "inspect_deployment",
    "load_model_artifact_catalog",
    "load_model_connections",
    "model_connections_path",
    "model_profile_from_mapping",
    "model_profile_from_preset",
    "model_settings_from_mapping",
    "model_settings_path",
    "persist_deployment_profile",
]
