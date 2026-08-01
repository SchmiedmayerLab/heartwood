# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Strict request models and typed JSON responses shared by every interface."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Literal, NotRequired, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, with_config

from heartwood.schemas._records import ActionConfirmationMode, CapabilityTier

__all__ = [
    "ActionConfirmationRequest",
    "ActionModeOptionResponse",
    "ActionPresentationResponse",
    "ActionSettingsResponse",
    "ApiRequest",
    "AuditExportResponse",
    "CredentialBindingStatusResponse",
    "CredentialIsolationResponse",
    "CredentialSettingsResponse",
    "CredentialStoreAvailabilityResponse",
    "CustomLocalModelDownloadRequest",
    "GpuCapacityResponse",
    "GpuEnvironmentResponse",
    "LocalModelChoiceResponse",
    "LocalModelImportRequest",
    "LocalModelImportResponse",
    "ModelArtifactResponse",
    "ModelArtifactsResponse",
    "ModelCatalogEntryResponse",
    "ModelCatalogRequest",
    "ModelCatalogResponse",
    "ModelConnectRequest",
    "ModelConnectionResponse",
    "ModelDownloadRequest",
    "ModelDownloadResponse",
    "ModelPresetResponse",
    "ModelProfileRequest",
    "ModelProfileResponse",
    "ModelRepositoryPlanResponse",
    "ModelRepositoryRequest",
    "ModelSelectionRequest",
    "ModelSettingsResponse",
    "ModelSnapshotResponse",
    "ModelSourceOptionResponse",
    "ModelSourceRequest",
    "ModelValidationResponse",
    "PlatformCapabilitiesResponse",
    "PolicyDecisionResponse",
    "ProjectReadinessResponse",
    "ReadinessCheckResponse",
    "SessionCreateRequest",
    "SessionListResponse",
    "SessionRenameRequest",
    "SessionSummaryResponse",
    "SkillInspectRequest",
    "SkillInstallRequest",
    "SkillSettingsResponse",
    "SkillSummaryResponse",
    "StartupPlanResponse",
    "SubscriptionDeviceLoginRequest",
    "SubscriptionDeviceLoginResponse",
    "SubscriptionDevicePollRequest",
    "api_contract_schema",
    "api_response",
]

type CredentialKind = Literal["environment", "file", "managed-identity", "none"]
type CredentialStatus = Literal["available", "configured", "missing"]
type CredentialIsolationStatus = Literal[
    "not-configured",
    "not-required",
    "review-required",
    "qualified",
]
type CredentialIsolationBoundary = Literal[
    "none",
    "credential-free",
    "application-scrubbed",
    "platform-isolated",
]
type InterfaceKind = Literal["terminal", "web", "notebook"]
type ActionRisk = Literal["high", "low", "medium", "unknown"]
type LocalModelQualification = Literal["unvalidated", "qualified"]
type LocalModelRuntime = Literal["llama-cpp", "vllm"]
type LocalModelTier = Literal["standard", "powerful", "maximum"]
type ModelSource = Literal[
    "anthropic",
    "custom",
    "heartwood",
    "openai",
    "openai-subscription",
    "stanford-ai-api-gateway",
]
type ToolCallParser = Literal["hermes", "openai", "qwen3_coder"]


class ApiRequest(BaseModel):
    """Base for immutable request bodies accepted at public boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SessionCreateRequest(ApiRequest):
    """Create one session with an optional title."""

    title: str | None = None


class SessionRenameRequest(ApiRequest):
    """Rename one existing session."""

    title: str = Field(min_length=1)


class ActionConfirmationRequest(ApiRequest):
    """Select the shared action-confirmation policy."""

    mode: ActionConfirmationMode


class ModelDownloadRequest(ApiRequest):
    """Download one catalog model."""

    model_id: str = Field(min_length=1)


class ModelRepositoryRequest(ApiRequest):
    """Inspect one Hugging Face model repository."""

    repository: str = Field(min_length=1)
    revision: str | None = None


class CustomLocalModelDownloadRequest(ModelRepositoryRequest):
    """Download one automatically inspected Hugging Face model."""


class LocalModelImportRequest(ApiRequest):
    """Import reviewed local model weights into project storage."""

    path: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    license: str = Field(min_length=1)
    context_window: int | None = Field(default=None, ge=2_048)


class ModelSourceRequest(ApiRequest):
    """Select one approachable model-source path."""

    source_id: ModelSource


class ModelCatalogRequest(ApiRequest):
    """Discover models from one configured connection."""

    connection_id: str = Field(min_length=1)
    token: str | None = None
    base_url: str | None = None
    refresh: bool = False
    remember: bool = False


class ModelConnectRequest(ApiRequest):
    """Connect one discovered or manually entered model."""

    connection_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    token: str | None = None
    base_url: str | None = None
    manual: bool = False
    remember: bool = False


class ModelProfileRequest(ApiRequest):
    """Create or replace one API-safe model profile."""

    profile_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    policy_endpoint: str = Field(min_length=1)
    capability_tier: CapabilityTier = "supervised"
    base_url: str | None = None
    credential_kind: CredentialKind = "environment"
    auth_type: Literal["api_key", "subscription"] = "api_key"
    subscription_vendor: str | None = None
    api_key_env: str | None = None
    api_key_file: str | None = None
    api_version: str | None = None
    aws_region_name: str | None = None
    aws_profile_name: str | None = None
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    description: str | None = None


class SubscriptionDeviceLoginRequest(ApiRequest):
    """Start an explicitly accepted subscription device login."""

    connection_id: str = Field(min_length=1)
    terms_accepted: Literal[True]


class SubscriptionDevicePollRequest(ApiRequest):
    """Poll one subscription device login."""

    connection_id: str = Field(min_length=1)
    login_id: str = Field(min_length=1)


class ModelSelectionRequest(ApiRequest):
    """Select one saved model profile."""

    profile_id: str = Field(min_length=1)


class SkillInspectRequest(ApiRequest):
    """Inspect one mounted Skill source."""

    source: str = Field(min_length=1)


class SkillInstallRequest(SkillInspectRequest):
    """Install one explicitly approved Skill source."""

    approved: bool


@with_config(ConfigDict(extra="forbid", strict=True))
class _ApiResponse(TypedDict):
    """Inherited strict validation configuration for response mappings."""


class SessionSummaryResponse(_ApiResponse):
    """Researcher-facing session summary."""

    session_id: str
    title: str
    status: Literal["empty", "idle", "waiting", "paused", "error", "recovery-required"]
    created_at: str
    updated_at: str
    event_count: int


class SessionListResponse(_ApiResponse):
    """Ordered session collection."""

    sessions: list[SessionSummaryResponse]


class AuditExportResponse(_ApiResponse):
    """Downloadable scrubbed audit export."""

    filename: str
    content: str


class ActionModeOptionResponse(_ApiResponse):
    """One selectable confirmation mode."""

    mode: ActionConfirmationMode
    command_value: str
    label: str
    description: str
    automatic_risks: list[ActionRisk]
    reviewed_risks: list[ActionRisk]
    recommended: bool
    allowed: bool
    unavailable_reason: str | None


class ActionPresentationResponse(_ApiResponse):
    """Shared researcher-facing action terminology."""

    risk_labels: dict[str, str]
    tool_labels: dict[str, str]
    other_tool_label_template: str
    unknown_risk_label: str
    unknown_tool_label: str


class ActionSettingsResponse(_ApiResponse):
    """Shared action-confirmation settings."""

    schema_version: Literal["heartwood.action-settings.v1"]
    confirmation_mode: ActionConfirmationMode
    scope_description: str
    presentation: ActionPresentationResponse
    change_allowed: bool
    change_blocked_reason: str | None
    modes: list[ActionModeOptionResponse]


class ReadinessCheckResponse(_ApiResponse):
    """One project readiness result and optional recovery guidance."""

    check_id: str
    status: Literal["pass", "warning", "fail"]
    summary: str
    code: NotRequired[str]
    title: NotRequired[str]
    next_action: NotRequired[str]
    documentation_path: NotRequired[str]


class ProjectReadinessResponse(_ApiResponse):
    """Content-free project readiness projection."""

    state: Literal["ready", "setup-required", "compute-required", "recovery-required"]
    platform_id: str
    project_root: str
    state_root: str
    evidence: list[str]
    checks: list[ReadinessCheckResponse]


class PlatformCapabilitiesResponse(_ApiResponse):
    """Capabilities owned by one deployment adapter."""

    platform_id: str
    display_name: str
    interfaces: list[InterfaceKind]
    browser_route: Literal["direct", "jupyter-proxy", "unavailable"]
    ingress_modes: list[Literal["direct-loopback", "jupyter-proxy", "trusted-proxy"]]
    default_ingress_mode: Literal["direct-loopback", "jupyter-proxy", "trusted-proxy"]
    managed_runtimes: list[Literal["llama-cpp", "vllm"]]
    scheduler: Literal["none", "provisioned", "slurm"]
    persistent_storage: str
    credential_backends: list[Literal["process", "keyring", "mounted-file", "managed-identity"]]
    model_sources: list[
        Literal[
            "anthropic",
            "custom",
            "heartwood",
            "openai",
            "openai-subscription",
            "stanford-ai-api-gateway",
        ]
    ]
    platform_isolated_model_sources: list[
        Literal[
            "anthropic",
            "custom",
            "heartwood",
            "openai",
            "openai-subscription",
            "stanford-ai-api-gateway",
        ]
    ]
    managed_model_connections: list[str]
    validation_level: Literal["ci", "ci-and-live-synthetic"]


class StartupPlanResponse(_ApiResponse):
    """Shared startup decision for one interaction surface."""

    phase: Literal[
        "project-review",
        "connection-required",
        "credential-required",
        "model-required",
        "compute-required",
        "ready",
        "recovery-required",
    ]
    interface: InterfaceKind
    platform_id: str
    project_root: str
    state_root: str
    summary: str
    next_action: str
    access_url: str | None
    requires_compute: bool
    requires_confirmation: bool
    interface_supported: bool
    readiness: ProjectReadinessResponse
    capabilities: PlatformCapabilitiesResponse


class ModelProfileResponse(_ApiResponse):
    """API-safe model profile without credential material."""

    profile_id: str
    model: str
    policy_endpoint: str
    capability_tier: CapabilityTier
    base_url: str | None
    credential_kind: CredentialKind
    auth_type: Literal["api_key", "subscription"]
    subscription_vendor: str | None
    api_key_env: str | None
    api_key_file: str | None
    api_version: str | None
    aws_region_name: str | None
    aws_profile_name: str | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    description: str | None
    credential_status: NotRequired[CredentialStatus]


class ModelConnectionResponse(_ApiResponse):
    """API-safe model connection metadata."""

    connection_id: str
    label: str
    protocol: Literal["anthropic", "openai", "openai-compatible", "static", "subscription"]
    model_prefix: str
    source: Literal["built-in", "platform", "user"]
    credential_kind: CredentialKind
    policy_endpoint: str | None
    catalog_endpoint: str | None
    base_url: str | None
    api_key_env: str | None
    api_key_file: str | None
    api_version: str | None
    aws_region_name: str | None
    aws_profile_name: str | None
    description: str
    static_models: list[str]
    subscription_vendor: str | None
    group: Literal[
        "compatible-service",
        "heartwood-managed",
        "hosted-provider",
        "research-environment",
    ]
    group_label: str
    accepts_token: bool
    supports_login: bool
    auth_type: Literal["api_key", "subscription"]
    credential_status: CredentialStatus


class ModelCatalogEntryResponse(_ApiResponse):
    """One normalized provider model."""

    model_id: str
    display_name: str
    execution_model: str
    availability: Literal["available", "experimental", "unsupported"]
    reason: str
    context_window: int | None
    supports_tools: bool | None


class ModelCatalogResponse(_ApiResponse):
    """Discovered models for one connection."""

    schema_version: Literal["heartwood.model-catalog.v1"]
    connection: ModelConnectionResponse
    models: list[ModelCatalogEntryResponse]
    refreshed_at: int


class SubscriptionDeviceLoginResponse(_ApiResponse):
    """Non-secret subscription device-login state."""

    schema_version: Literal["heartwood.subscription-login.v1"]
    login_id: str
    connection_id: str
    verification_url: str
    user_code: str
    poll_interval_seconds: int
    status: Literal["pending", "complete"]


class CredentialStoreAvailabilityResponse(_ApiResponse):
    """Credential-store capabilities without credential values."""

    backends: list[Literal["process", "keyring"]]
    default_backend: Literal["process", "keyring"]
    persistence_available: bool
    persistence_description: str


class CredentialBindingStatusResponse(_ApiResponse):
    """Non-secret status for one credential binding."""

    binding_id: str
    configured: bool
    source: Literal["environment", "keyring", "process", "unavailable"] | None
    error: str | None


class CredentialSettingsResponse(_ApiResponse):
    """Credential-store and binding status."""

    store: CredentialStoreAvailabilityResponse
    bindings: list[CredentialBindingStatusResponse]


class CredentialIsolationResponse(_ApiResponse):
    """Model-authentication isolation relative to agent tools."""

    status: CredentialIsolationStatus
    boundary: CredentialIsolationBoundary
    unattended_actions_allowed: bool
    summary: str


class ModelPresetResponse(_ApiResponse):
    """Advanced non-secret provider defaults."""

    preset_id: str
    label: str
    model_prefix: str
    credential_kind: CredentialKind
    api_key_env: str | None
    base_url: str | None
    policy_endpoint: str | None
    description: str


class ModelSourceOptionResponse(_ApiResponse):
    """One approachable model-source option."""

    source_id: ModelSource
    connection_id: str
    label: str
    description: str
    selected: bool


class ModelSettingsResponse(_ApiResponse):
    """Complete API-safe model configuration."""

    schema_version: Literal["heartwood.model-settings.v1"]
    active_profile: str | None
    model_source: str | None
    profiles: list[ModelProfileResponse]
    connections: list[ModelConnectionResponse]
    presets: list[ModelPresetResponse]
    source_options: list[ModelSourceOptionResponse]
    credential_store: CredentialStoreAvailabilityResponse
    credential_bindings: list[CredentialBindingStatusResponse]
    credential_isolation: CredentialIsolationResponse


class PolicyDecisionResponse(_ApiResponse):
    """Relevant fields from a model policy decision."""

    schema_version: Literal["heartwood.model-call-decision.v1"]
    decision_id: str
    policy_profile_id: str
    decision: str
    endpoint: str
    capability_tier: CapabilityTier
    reason: str


class ModelValidationResponse(_ApiResponse):
    """Selected model, credential, confirmation, and policy validation."""

    profile: ModelProfileResponse
    credential_status: CredentialStatus
    credential_isolation: CredentialIsolationResponse
    action_confirmation_mode: ActionConfirmationMode
    policy_decision: PolicyDecisionResponse


class ModelArtifactResponse(_ApiResponse):
    """Pinned single-file local model metadata."""

    artifact_id: str
    runtime_profile: str
    purpose: str
    source_repository: str
    source_path: str
    source_revision: str
    artifact_format: str
    artifact_size_bytes: int
    minimum_free_bytes: int
    artifact_sha256: str
    license_posture: str
    model_alias: str
    context_window: int
    minimum_resource_envelope: str | None
    recommended_resource_envelope: str | None
    qualification: LocalModelQualification
    validated_platforms: list[str]
    qualification_test: str | None
    qualification_date: str | None
    qualification_evidence: str | None
    recommended: bool


class ModelDownloadResponse(_ApiResponse):
    """Background model download status."""

    model_id: str
    status: Literal["downloading", "error", "ready"]
    bytes_downloaded: int
    bytes_total: int
    path: str | None
    error: str | None


class LocalModelChoiceResponse(_ApiResponse):
    """One normalized local model choice."""

    model_id: str
    label: str
    purpose: str
    runtime: LocalModelRuntime
    source_repository: str
    source_revision: str
    source_path: str | None
    size_bytes: int
    minimum_free_bytes: int
    license_id: str
    license_posture: str
    catalog_source: Literal["catalog", "user-selected"]
    model_type: str | None
    context_window: int
    maximum_context_window: int
    precision: str
    tier: LocalModelTier
    qualification: LocalModelQualification
    minimum_gpu_count: int
    minimum_gpu_memory_bytes: int
    recommended_cpu_count: int
    recommended_ram_bytes: int
    recommended_disk_bytes: int
    tool_call_parser: ToolCallParser | None
    tensor_parallel_size: int
    startup_seconds_min: int
    startup_seconds_max: int
    download_policy: str | None
    allow_patterns: list[str]
    ignore_patterns: list[str]
    validated_platforms: list[str]
    qualification_test: str | None
    qualification_date: str | None
    qualification_evidence: str | None
    artifact_sha256: str | None
    minimum_resource_envelope: str | None
    recommended_resource_envelope: str | None
    active: bool
    available: bool
    selected: bool
    availability_reason: str
    recommended: bool


class GpuCapacityResponse(_ApiResponse):
    """One currently visible or requestable GPU capacity."""

    label: str
    gpu_model: str
    gpu_count: int
    gpu_memory_bytes: int
    allocation_required: bool
    partition: str | None


class GpuEnvironmentResponse(_ApiResponse):
    """Platform GPU capacities used for local-model recommendations."""

    platform_id: str
    capacities: list[GpuCapacityResponse]


class ModelRepositoryPlanResponse(_ApiResponse):
    """Automatic runtime and resource plan for a model repository."""

    model: LocalModelChoiceResponse
    selection_reason: str


class LocalModelImportResponse(_ApiResponse):
    """Completed local-model import."""

    model: LocalModelChoiceResponse
    path: str
    status: Literal["ready"]


class ModelSnapshotResponse(_ApiResponse):
    """Pinned multi-file local model metadata."""

    snapshot_id: str
    runtime_profile: str
    purpose: str
    source_repository: str
    source_revision: str
    expected_size_bytes: int
    minimum_free_bytes: int
    license_id: str
    license_posture: str
    model_alias: str
    precision: str
    tier: LocalModelTier
    qualification: LocalModelQualification
    minimum_gpu_count: int
    minimum_gpu_memory_bytes: int
    recommended_cpu_count: int
    recommended_ram_bytes: int
    recommended_disk_bytes: int
    context_window: int
    maximum_context_window: int
    tool_call_parser: ToolCallParser
    tensor_parallel_size: int
    startup_seconds_min: int
    startup_seconds_max: int
    download_policy: str
    allow_patterns: list[str]
    ignore_patterns: list[str]
    validated_platforms: list[str]
    qualification_test: str | None
    qualification_date: str | None
    qualification_evidence: str | None
    minimum_resource_envelope: str | None
    recommended_resource_envelope: str | None
    recommended: bool


class ModelArtifactsResponse(_ApiResponse):
    """Complete local model catalog and current environment status."""

    schema_version: Literal["heartwood.local-model-catalog.v2"]
    snapshot_schema_version: Literal["heartwood.model-snapshot-catalog.v3"]
    artifacts: list[ModelArtifactResponse]
    snapshots: list[ModelSnapshotResponse]
    models: list[LocalModelChoiceResponse]
    downloads: list[ModelDownloadResponse]
    gpu_environment: GpuEnvironmentResponse


class SkillSummaryResponse(_ApiResponse):
    """One bundled, candidate, or installed Skill."""

    name: str
    skill_id: str
    description: str
    trust_tier: str
    source: Literal["bundled", "candidate", "installed"]
    approval_summary: str
    declared_tools: list[str]
    requires_network: bool


class SkillSettingsResponse(_ApiResponse):
    """Bundled and explicitly installed Skills."""

    skills: list[SkillSummaryResponse]


type ApiResponse = (
    ActionSettingsResponse
    | AuditExportResponse
    | CredentialSettingsResponse
    | LocalModelImportResponse
    | ModelArtifactsResponse
    | ModelCatalogResponse
    | ModelDownloadResponse
    | ModelRepositoryPlanResponse
    | ModelSettingsResponse
    | ModelValidationResponse
    | PlatformCapabilitiesResponse
    | ProjectReadinessResponse
    | SessionListResponse
    | SessionSummaryResponse
    | SkillSettingsResponse
    | SkillSummaryResponse
    | StartupPlanResponse
    | SubscriptionDeviceLoginResponse
)

type PublicApiContract = (
    ApiResponse
    | ActionConfirmationRequest
    | CustomLocalModelDownloadRequest
    | LocalModelImportRequest
    | ModelCatalogRequest
    | ModelConnectRequest
    | ModelDownloadRequest
    | ModelProfileRequest
    | ModelRepositoryRequest
    | ModelSelectionRequest
    | ModelSourceRequest
    | SessionCreateRequest
    | SessionRenameRequest
    | SkillInspectRequest
    | SkillInstallRequest
    | SubscriptionDeviceLoginRequest
    | SubscriptionDevicePollRequest
)

_PUBLIC_API_CONTRACT_ADAPTER: TypeAdapter[PublicApiContract] = TypeAdapter(PublicApiContract)
_RESPONSE_ADAPTERS: dict[type[object], object] = {}


def api_response[ResponseT: ApiResponse](
    response_type: type[ResponseT],
    value: object,
) -> ResponseT:
    """Validate one API response and preserve its precise mapping type."""
    serialized = json.dumps(value, separators=(",", ":"))
    return _response_adapter(response_type).validate_json(serialized, strict=True)


def _response_adapter[ResponseT: ApiResponse](
    response_type: type[ResponseT],
) -> TypeAdapter[ResponseT]:
    """Compile each response validator once per process."""
    cached = _RESPONSE_ADAPTERS.get(response_type)
    if cached is not None:
        return cast(TypeAdapter[ResponseT], cached)
    adapter = TypeAdapter(response_type)
    _RESPONSE_ADAPTERS[response_type] = adapter
    return adapter


def api_contract_schema() -> dict[str, JsonValue]:
    """Return the complete browser-facing JSON Schema contract."""
    return deepcopy(_PUBLIC_API_CONTRACT_ADAPTER.json_schema(mode="serialization"))
