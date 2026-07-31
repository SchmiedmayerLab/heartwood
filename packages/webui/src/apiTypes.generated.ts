/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* eslint-disable */
/**
 * Generated from the public Pydantic API contract.
 * Run `npm run contracts:generate` after changing a shared request or response.
 */

export type HeartwoodApiContract =
  | ApiResponse
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
  | SubscriptionDevicePollRequest;
export type ApiResponse =
  | ActionSettingsResponse
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
  | WorkspaceChangesResponse
  | WorkspaceDiffResponse
  | WorkspaceFileResponse
  | WorkspaceTreeResponse;
export type ActionConfirmationMode = "always-confirm" | "confirm-risky";
export type ActionRisk = "high" | "low" | "medium" | "unknown";
export type LocalModelQualification = "unvalidated" | "qualified";
export type LocalModelRuntime = "llama-cpp" | "vllm";
export type LocalModelTier = "standard" | "powerful" | "maximum";
export type ToolCallParser = "hermes" | "openai" | "qwen3_coder";
export type CredentialKind =
  "environment" | "file" | "managed-identity" | "none";
export type CredentialStatus = "available" | "configured" | "missing";
export type CapabilityTier = "autonomous" | "supervised" | "experimental";
export type ModelSource =
  | "anthropic"
  | "custom"
  | "heartwood"
  | "openai"
  | "openai-subscription"
  | "stanford-ai-api-gateway";
export type InterfaceKind = "terminal" | "web" | "notebook";

/**
 * Shared action-confirmation settings.
 */
export interface ActionSettingsResponse {
  change_allowed: boolean;
  change_blocked_reason: string | null;
  confirmation_mode: ActionConfirmationMode;
  modes: ActionModeOptionResponse[];
  presentation: ActionPresentationResponse;
  schema_version: "heartwood.action-settings.v1";
  scope_description: string;
}
/**
 * One selectable confirmation mode.
 */
export interface ActionModeOptionResponse {
  allowed: boolean;
  automatic_risks: ActionRisk[];
  command_value: string;
  description: string;
  label: string;
  mode: ActionConfirmationMode;
  recommended: boolean;
  reviewed_risks: ActionRisk[];
  unavailable_reason: string | null;
}
/**
 * Shared researcher-facing action terminology.
 */
export interface ActionPresentationResponse {
  other_tool_label_template: string;
  risk_labels: {
    [k: string]: string;
  };
  tool_labels: {
    [k: string]: string;
  };
  unknown_risk_label: string;
  unknown_tool_label: string;
}
/**
 * Downloadable scrubbed audit export.
 */
export interface AuditExportResponse {
  content: string;
  filename: string;
}
/**
 * Credential-store and binding status.
 */
export interface CredentialSettingsResponse {
  bindings: CredentialBindingStatusResponse[];
  store: CredentialStoreAvailabilityResponse;
}
/**
 * Non-secret status for one credential binding.
 */
export interface CredentialBindingStatusResponse {
  binding_id: string;
  configured: boolean;
  error: string | null;
  source: ("environment" | "keyring" | "process" | "unavailable") | null;
}
/**
 * Credential-store capabilities without credential values.
 */
export interface CredentialStoreAvailabilityResponse {
  backends: ("process" | "keyring")[];
  default_backend: "process" | "keyring";
  persistence_available: boolean;
  persistence_description: string;
}
/**
 * Completed local-model import.
 */
export interface LocalModelImportResponse {
  model: LocalModelChoiceResponse;
  path: string;
  status: "ready";
}
/**
 * One normalized local model choice.
 */
export interface LocalModelChoiceResponse {
  active: boolean;
  allow_patterns: string[];
  artifact_sha256: string | null;
  availability_reason: string;
  available: boolean;
  catalog_source: "catalog" | "user-selected";
  context_window: number;
  download_policy: string | null;
  ignore_patterns: string[];
  label: string;
  license_id: string;
  license_posture: string;
  maximum_context_window: number;
  minimum_free_bytes: number;
  minimum_gpu_count: number;
  minimum_gpu_memory_bytes: number;
  minimum_resource_envelope: string | null;
  model_id: string;
  model_type: string | null;
  precision: string;
  purpose: string;
  qualification: LocalModelQualification;
  qualification_date: string | null;
  qualification_evidence: string | null;
  qualification_test: string | null;
  recommended: boolean;
  recommended_cpu_count: number;
  recommended_disk_bytes: number;
  recommended_ram_bytes: number;
  recommended_resource_envelope: string | null;
  runtime: LocalModelRuntime;
  selected: boolean;
  size_bytes: number;
  source_path: string | null;
  source_repository: string;
  source_revision: string;
  startup_seconds_max: number;
  startup_seconds_min: number;
  tensor_parallel_size: number;
  tier: LocalModelTier;
  tool_call_parser: ToolCallParser | null;
  validated_platforms: string[];
}
/**
 * Complete local model catalog and current environment status.
 */
export interface ModelArtifactsResponse {
  artifacts: ModelArtifactResponse[];
  downloads: ModelDownloadResponse[];
  gpu_environment: GpuEnvironmentResponse;
  models: LocalModelChoiceResponse[];
  schema_version: "heartwood.local-model-catalog.v2";
  snapshot_schema_version: "heartwood.model-snapshot-catalog.v3";
  snapshots: ModelSnapshotResponse[];
}
/**
 * Pinned single-file local model metadata.
 */
export interface ModelArtifactResponse {
  artifact_format: string;
  artifact_id: string;
  artifact_sha256: string;
  artifact_size_bytes: number;
  context_window: number;
  license_posture: string;
  minimum_free_bytes: number;
  minimum_resource_envelope: string | null;
  model_alias: string;
  purpose: string;
  qualification: LocalModelQualification;
  qualification_date: string | null;
  qualification_evidence: string | null;
  qualification_test: string | null;
  recommended: boolean;
  recommended_resource_envelope: string | null;
  runtime_profile: string;
  source_path: string;
  source_repository: string;
  source_revision: string;
  validated_platforms: string[];
}
/**
 * Background model download status.
 */
export interface ModelDownloadResponse {
  bytes_downloaded: number;
  bytes_total: number;
  error: string | null;
  model_id: string;
  path: string | null;
  status: "downloading" | "error" | "ready";
}
/**
 * Platform GPU capacities used for local-model recommendations.
 */
export interface GpuEnvironmentResponse {
  capacities: GpuCapacityResponse[];
  platform_id: string;
}
/**
 * One currently visible or requestable GPU capacity.
 */
export interface GpuCapacityResponse {
  allocation_required: boolean;
  gpu_count: number;
  gpu_memory_bytes: number;
  gpu_model: string;
  label: string;
  partition: string | null;
}
/**
 * Pinned multi-file local model metadata.
 */
export interface ModelSnapshotResponse {
  allow_patterns: string[];
  context_window: number;
  download_policy: string;
  expected_size_bytes: number;
  ignore_patterns: string[];
  license_id: string;
  license_posture: string;
  maximum_context_window: number;
  minimum_free_bytes: number;
  minimum_gpu_count: number;
  minimum_gpu_memory_bytes: number;
  minimum_resource_envelope: string | null;
  model_alias: string;
  precision: string;
  purpose: string;
  qualification: LocalModelQualification;
  qualification_date: string | null;
  qualification_evidence: string | null;
  qualification_test: string | null;
  recommended: boolean;
  recommended_cpu_count: number;
  recommended_disk_bytes: number;
  recommended_ram_bytes: number;
  recommended_resource_envelope: string | null;
  runtime_profile: string;
  snapshot_id: string;
  source_repository: string;
  source_revision: string;
  startup_seconds_max: number;
  startup_seconds_min: number;
  tensor_parallel_size: number;
  tier: LocalModelTier;
  tool_call_parser: ToolCallParser;
  validated_platforms: string[];
}
/**
 * Discovered models for one connection.
 */
export interface ModelCatalogResponse {
  connection: ModelConnectionResponse;
  models: ModelCatalogEntryResponse[];
  refreshed_at: number;
  schema_version: "heartwood.model-catalog.v1";
}
/**
 * API-safe model connection metadata.
 */
export interface ModelConnectionResponse {
  accepts_token: boolean;
  api_key_env: string | null;
  api_key_file: string | null;
  api_version: string | null;
  auth_type: "api_key" | "subscription";
  aws_profile_name: string | null;
  aws_region_name: string | null;
  base_url: string | null;
  catalog_endpoint: string | null;
  connection_id: string;
  credential_kind: CredentialKind;
  credential_status: CredentialStatus;
  description: string;
  group:
    | "compatible-service"
    | "heartwood-managed"
    | "hosted-provider"
    | "research-environment";
  group_label: string;
  label: string;
  model_prefix: string;
  policy_endpoint: string | null;
  protocol:
    "anthropic" | "openai" | "openai-compatible" | "static" | "subscription";
  source: "built-in" | "platform" | "user";
  static_models: string[];
  subscription_vendor: string | null;
  supports_login: boolean;
}
/**
 * One normalized provider model.
 */
export interface ModelCatalogEntryResponse {
  availability: "available" | "experimental" | "unsupported";
  context_window: number | null;
  display_name: string;
  execution_model: string;
  model_id: string;
  reason: string;
  supports_tools: boolean | null;
}
/**
 * Automatic runtime and resource plan for a model repository.
 */
export interface ModelRepositoryPlanResponse {
  model: LocalModelChoiceResponse;
  selection_reason: string;
}
/**
 * Complete API-safe model configuration.
 */
export interface ModelSettingsResponse {
  active_profile: string | null;
  connections: ModelConnectionResponse[];
  credential_bindings: CredentialBindingStatusResponse[];
  credential_store: CredentialStoreAvailabilityResponse;
  model_source: string | null;
  presets: ModelPresetResponse[];
  profiles: ModelProfileResponse[];
  schema_version: "heartwood.model-settings.v1";
  source_options: ModelSourceOptionResponse[];
}
/**
 * Advanced non-secret provider defaults.
 */
export interface ModelPresetResponse {
  api_key_env: string | null;
  base_url: string | null;
  credential_kind: CredentialKind;
  description: string;
  label: string;
  model_prefix: string;
  policy_endpoint: string | null;
  preset_id: string;
}
/**
 * API-safe model profile without credential material.
 */
export interface ModelProfileResponse {
  api_key_env: string | null;
  api_key_file: string | null;
  api_version: string | null;
  auth_type: "api_key" | "subscription";
  aws_profile_name: string | null;
  aws_region_name: string | null;
  base_url: string | null;
  capability_tier: CapabilityTier;
  credential_kind: CredentialKind;
  credential_status?: CredentialStatus;
  description: string | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  model: string;
  policy_endpoint: string;
  profile_id: string;
  subscription_vendor: string | null;
}
/**
 * One approachable model-source option.
 */
export interface ModelSourceOptionResponse {
  connection_id: string;
  description: string;
  label: string;
  selected: boolean;
  source_id: ModelSource;
}
/**
 * Selected model, credential, confirmation, and policy validation.
 */
export interface ModelValidationResponse {
  action_confirmation_mode: ActionConfirmationMode;
  credential_status: CredentialStatus;
  policy_decision: PolicyDecisionResponse;
  profile: ModelProfileResponse;
}
/**
 * Relevant fields from a model policy decision.
 */
export interface PolicyDecisionResponse {
  capability_tier: CapabilityTier;
  decision: string;
  decision_id: string;
  endpoint: string;
  policy_profile_id: string;
  reason: string;
  schema_version: "heartwood.model-call-decision.v1";
}
/**
 * Capabilities owned by one deployment adapter.
 */
export interface PlatformCapabilitiesResponse {
  browser_route: "direct" | "jupyter-proxy" | "unavailable";
  credential_backends: (
    "process" | "keyring" | "mounted-file" | "managed-identity"
  )[];
  display_name: string;
  interfaces: InterfaceKind[];
  managed_model_connections: string[];
  managed_runtimes: ("llama-cpp" | "vllm")[];
  model_sources: (
    | "anthropic"
    | "custom"
    | "heartwood"
    | "openai"
    | "openai-subscription"
    | "stanford-ai-api-gateway"
  )[];
  persistent_storage: string;
  platform_id: string;
  scheduler: "none" | "provisioned" | "slurm";
  validation_level: "ci" | "ci-and-live-synthetic";
}
/**
 * Content-free project readiness projection.
 */
export interface ProjectReadinessResponse {
  checks: ReadinessCheckResponse[];
  evidence: string[];
  platform_id: string;
  project_root: string;
  state: "ready" | "setup-required" | "compute-required" | "recovery-required";
  state_root: string;
}
/**
 * One project readiness result and optional recovery guidance.
 */
export interface ReadinessCheckResponse {
  check_id: string;
  code?: string;
  documentation_path?: string;
  next_action?: string;
  status: "pass" | "warning" | "fail";
  summary: string;
  title?: string;
}
/**
 * Ordered session collection.
 */
export interface SessionListResponse {
  sessions: SessionSummaryResponse[];
}
/**
 * Researcher-facing session summary.
 */
export interface SessionSummaryResponse {
  created_at: string;
  event_count: number;
  session_id: string;
  status:
    "empty" | "idle" | "waiting" | "paused" | "error" | "recovery-required";
  title: string;
  updated_at: string;
}
/**
 * Bundled and explicitly installed Skills.
 */
export interface SkillSettingsResponse {
  skills: SkillSummaryResponse[];
}
/**
 * One bundled, candidate, or installed Skill.
 */
export interface SkillSummaryResponse {
  approval_summary: string;
  declared_tools: string[];
  description: string;
  name: string;
  requires_network: boolean;
  skill_id: string;
  source: "bundled" | "candidate" | "installed";
  trust_tier: string;
}
/**
 * Shared startup decision for one interaction surface.
 */
export interface StartupPlanResponse {
  access_url: string | null;
  capabilities: PlatformCapabilitiesResponse;
  interface: InterfaceKind;
  interface_supported: boolean;
  next_action: string;
  phase:
    | "project-review"
    | "connection-required"
    | "credential-required"
    | "model-required"
    | "compute-required"
    | "ready"
    | "recovery-required";
  platform_id: string;
  project_root: string;
  readiness: ProjectReadinessResponse;
  requires_compute: boolean;
  requires_confirmation: boolean;
  state_root: string;
  summary: string;
}
/**
 * Non-secret subscription device-login state.
 */
export interface SubscriptionDeviceLoginResponse {
  connection_id: string;
  login_id: string;
  poll_interval_seconds: number;
  schema_version: "heartwood.subscription-login.v1";
  status: "pending" | "complete";
  user_code: string;
  verification_url: string;
}
/**
 * Bounded changed-file list for one project and session.
 */
export interface WorkspaceChangesResponse {
  changes: WorkspaceChangeResponse[];
  limits: WorkspaceLimitsResponse;
  message: string | null;
  schema_version: "heartwood.workspace-changes.v1";
  source: "git" | "session-actions" | "unavailable";
  status: "available" | "non-git" | "truncated" | "unavailable" | "unsupported";
  truncated: boolean;
}
/**
 * One changed project path from Git or structured session evidence.
 */
export interface WorkspaceChangeResponse {
  action_ids: string[];
  path: string;
  source: "git" | "session-action";
  status: "added" | "deleted" | "modified";
}
/**
 * Applied workspace-inspection limits.
 */
export interface WorkspaceLimitsResponse {
  max_change_entries: number;
  max_diff_bytes: number;
  max_file_bytes: number;
  max_file_lines: number;
  max_tree_depth: number;
  max_tree_entries: number;
}
/**
 * Bounded read-only original and modified file contents.
 */
export interface WorkspaceDiffResponse {
  message: string | null;
  modified: string | null;
  original: string | null;
  path: string;
  schema_version: "heartwood.workspace-diff.v1";
  source: "git" | "session-action" | "unavailable";
  status:
    | "available"
    | "binary"
    | "truncated"
    | "unavailable"
    | "non-git"
    | "unsupported";
  truncated: boolean;
}
/**
 * Bounded read-only project file.
 */
export interface WorkspaceFileResponse {
  bytes_read: number;
  content: string | null;
  line_count: number;
  message: string | null;
  path: string;
  schema_version: "heartwood.workspace-file.v1";
  size_bytes: number | null;
  status: "available" | "binary" | "truncated" | "unavailable" | "unsupported";
  truncated: boolean;
}
/**
 * Bounded project tree with private state removed.
 */
export interface WorkspaceTreeResponse {
  entries: WorkspaceTreeEntryResponse[];
  limits: WorkspaceLimitsResponse;
  path: string;
  schema_version: "heartwood.workspace-tree.v1";
  status: "available" | "truncated";
  truncated: boolean;
}
/**
 * One safe project entry in a bounded workspace tree.
 */
export interface WorkspaceTreeEntryResponse {
  depth: number;
  kind: "directory" | "file" | "unsupported";
  name: string;
  path: string;
  size_bytes: number | null;
}
/**
 * Select the shared action-confirmation policy.
 */
export interface ActionConfirmationRequest {
  mode: ActionConfirmationMode;
}
/**
 * Download one automatically inspected Hugging Face model.
 */
export interface CustomLocalModelDownloadRequest {
  repository: string;
  revision?: string | null;
}
/**
 * Import reviewed local model weights into project storage.
 */
export interface LocalModelImportRequest {
  context_window?: number | null;
  license: string;
  path: string;
  repository: string;
  revision: string;
}
/**
 * Discover models from one configured connection.
 */
export interface ModelCatalogRequest {
  base_url?: string | null;
  connection_id: string;
  refresh?: boolean;
  remember?: boolean;
  token?: string | null;
}
/**
 * Connect one discovered or manually entered model.
 */
export interface ModelConnectRequest {
  base_url?: string | null;
  connection_id: string;
  manual?: boolean;
  model_id: string;
  remember?: boolean;
  token?: string | null;
}
/**
 * Download one catalog model.
 */
export interface ModelDownloadRequest {
  model_id: string;
}
/**
 * Create or replace one API-safe model profile.
 */
export interface ModelProfileRequest {
  api_key_env?: string | null;
  api_key_file?: string | null;
  api_version?: string | null;
  auth_type?: "api_key" | "subscription";
  aws_profile_name?: string | null;
  aws_region_name?: string | null;
  base_url?: string | null;
  capability_tier?: "autonomous" | "supervised" | "experimental";
  credential_kind?: "environment" | "file" | "managed-identity" | "none";
  description?: string | null;
  max_input_tokens?: number | null;
  max_output_tokens?: number | null;
  model: string;
  policy_endpoint: string;
  profile_id: string;
  subscription_vendor?: string | null;
}
/**
 * Inspect one Hugging Face model repository.
 */
export interface ModelRepositoryRequest {
  repository: string;
  revision?: string | null;
}
/**
 * Select one saved model profile.
 */
export interface ModelSelectionRequest {
  profile_id: string;
}
/**
 * Select one approachable model-source path.
 */
export interface ModelSourceRequest {
  source_id: ModelSource;
}
/**
 * Create one session with an optional title.
 */
export interface SessionCreateRequest {
  title?: string | null;
}
/**
 * Rename one existing session.
 */
export interface SessionRenameRequest {
  title: string;
}
/**
 * Inspect one mounted Skill source.
 */
export interface SkillInspectRequest {
  source: string;
}
/**
 * Install one explicitly approved Skill source.
 */
export interface SkillInstallRequest {
  approved: boolean;
  source: string;
}
/**
 * Start an explicitly accepted subscription device login.
 */
export interface SubscriptionDeviceLoginRequest {
  connection_id: string;
  terms_accepted: true;
}
/**
 * Poll one subscription device login.
 */
export interface SubscriptionDevicePollRequest {
  connection_id: string;
  login_id: string;
}
