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
  | ExperimentCollection
  | PlanningProjection
  | PlanningEstimateRequest
  | PythonEnvironmentSnapshot
  | ExperimentExport
  | WorkflowCatalog
  | WorkflowRequest
  | ActionReviewRequest
  | CustomLocalModelDownloadRequest
  | LocalModelImportRequest
  | McpServerAddRequest
  | McpServerEnabledRequest
  | McpServerTestRequest
  | McpServerToolsRequest
  | McpToolChoice
  | ModelCatalogRequest
  | ModelConnectRequest
  | ModelDownloadRequest
  | ModelProfileRequest
  | ModelRepositoryRequest
  | ModelSelectionRequest
  | ModelSourceRequest
  | ModelTransferExportRequest
  | ModelTransferImportRequest
  | ModelTransferInspectRequest
  | SessionCreateRequest
  | SessionRenameRequest
  | SkillInspectRequest
  | SkillInstallRequest
  | SkillLocalInspectRequest
  | SkillLocalInstallRequest
  | SkillRefreshRequest
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
  | ModelTransferPlanResponse
  | ModelTransferResponse
  | ModelValidationResponse
  | PlatformCapabilitiesResponse
  | ProjectReadinessResponse
  | SessionListResponse
  | SessionSummaryResponse
  | McpServerResponse
  | McpServersResponse
  | McpServerTestResponse
  | SkillSettingsResponse
  | SkillSummaryResponse
  | SpecialistSettingsResponse
  | StartupPlanResponse
  | SubscriptionDeviceLoginResponse
  | WorkspaceChangesResponse
  | WorkspaceDiffResponse
  | WorkspaceFileResponse
  | WorkspaceTreeResponse;
export type ActionReviewMode =
  "review-all" | "review-changes" | "review-outside-effects";
export type LocalModelQualification = "unvalidated" | "qualified";
export type ReasoningParser = "muse_glimmer";
export type LocalModelRuntime = "llama-cpp" | "vllm";
export type LocalModelTier = "standard" | "powerful" | "maximum";
export type ToolCallParser =
  "hermes" | "muse_glimmer" | "openai" | "qwen3_coder";
export type CredentialKind =
  "environment" | "file" | "managed-identity" | "none";
export type CredentialStatus = "available" | "configured" | "missing";
export type DataDestination =
  "same-compute" | "institution-managed" | "provider" | "operator-defined";
export type RetentionAuthority =
  "deployment" | "institution" | "provider" | "service-operator";
export type CredentialIsolationBoundary =
  "none" | "credential-free" | "application-scrubbed" | "platform-isolated";
export type CredentialIsolationStatus =
  "not-configured" | "not-required" | "review-required" | "qualified";
export type CapabilityTier = "autonomous" | "supervised" | "experimental";
export type ModelSource =
  | "anthropic"
  | "custom"
  | "heartwood"
  | "openai"
  | "openai-subscription"
  | "stanford-ai-api-gateway";
export type InterfaceKind = "terminal" | "web" | "notebook";
export type Reference = string;
export type ExperimentPath = string;
export type Digest = string;
export type ExperimentStatus =
  "started" | "interrupted" | "resumed" | "succeeded" | "failed" | "cancelled";
export type PlanningServiceStatus =
  "unchecked" | "available" | "not-offered" | "unavailable" | "refused";
export type MetadataText = string;
export type WorkflowIdentifier = string;
export type WorkflowText = string;
export type WorkflowRequest =
  | WorkflowStart
  | WorkflowTransition
  | WorkflowReviewRequest
  | WorkflowReview
  | WorkflowCorrectionRequest;
export type WorkflowInputValue = string;

/**
 * The review mode for new action sets and each mode the deployment permits.
 */
export interface ActionSettingsResponse {
  change_allowed: boolean;
  change_blocked_reason: string | null;
  default_mode: ActionReviewMode;
  modes: ActionModeOptionResponse[];
  presentation: ActionPresentationResponse;
  review_mode: ActionReviewMode;
  schema_version: "heartwood.action-settings.v1";
  scope: "project" | "launch";
  scope_description: string;
}
/**
 * One review mode, the rules it applies automatically, and its availability.
 */
export interface ActionModeOptionResponse {
  allowed: boolean;
  automatic: string[];
  description: string;
  label: string;
  mode: ActionReviewMode;
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
  state_labels: {
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
  default_backend: "process" | "keyring" | "deployment";
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
  catalog_source: "catalog" | "transferred" | "user-selected";
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
  reasoning_parser: ReasoningParser | null;
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
  transfers: ModelTransferResponse[];
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
  tier: LocalModelTier;
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
  reasoning_parser: ReasoningParser | null;
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
 * Background model export or import status.
 */
export interface ModelTransferResponse {
  bundle_path: string;
  bytes_processed: number;
  bytes_total: number;
  error: string | null;
  kind: "export" | "import";
  label: string;
  model_id: string;
  phase:
    | "preparing"
    | "verifying"
    | "exporting"
    | "importing"
    | "selecting"
    | "complete";
  result_path: string | null;
  sequence: number;
  status: "cancelled" | "cancelling" | "error" | "ready" | "running";
  transfer_id: string;
  warnings: string[];
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
  data_handling: ModelDataHandlingResponse | null;
  data_handling_summary: string;
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
 * Declared content destination and retention authority of one model route.
 */
export interface ModelDataHandlingResponse {
  destination: DataDestination;
  retention_authority: RetentionAuthority;
  route: string;
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
  credential_isolation: CredentialIsolationResponse;
  credential_store: CredentialStoreAvailabilityResponse;
  data_handling: ModelDataHandlingResponse | null;
  data_handling_summary: string;
  model_source: string | null;
  presets: ModelPresetResponse[];
  profiles: ModelProfileResponse[];
  schema_version: "heartwood.model-settings.v1";
  source_options: ModelSourceOptionResponse[];
}
/**
 * Model-authentication isolation relative to agent tools.
 */
export interface CredentialIsolationResponse {
  boundary: CredentialIsolationBoundary;
  status: CredentialIsolationStatus;
  summary: string;
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
 * Content-safe bundle metadata shown before import approval.
 */
export interface ModelTransferPlanResponse {
  bundle_path: string;
  bundle_size_bytes: number;
  file_count: number;
  manifest_sha256: string;
  model: LocalModelChoiceResponse;
  runtime_profile: "llama-cpp-cpu" | "vllm-cuda";
  warnings: string[];
}
/**
 * Selected model, credential, confirmation, and policy validation.
 */
export interface ModelValidationResponse {
  action_review_mode: ActionReviewMode;
  credential_isolation: CredentialIsolationResponse;
  credential_status: CredentialStatus;
  data_handling: ModelDataHandlingResponse | null;
  data_handling_summary: string;
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
  default_ingress_mode:
    "direct-loopback" | "jupyter-proxy" | "trusted-proxy" | "identity-proxy";
  display_name: string;
  ingress_modes: (
    "direct-loopback" | "jupyter-proxy" | "trusted-proxy" | "identity-proxy"
  )[];
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
  platform_isolated_model_sources: (
    | "anthropic"
    | "custom"
    | "heartwood"
    | "openai"
    | "openai-subscription"
    | "stanford-ai-api-gateway"
  )[];
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
  policy: EffectivePolicyResponse;
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
 * The policy in force for a project and the trusted configuration that bounds it.
 */
export interface EffectivePolicyResponse {
  action_review_modes: ActionReviewMode[];
  aggregate_count_floor: number;
  capability_tiers: CapabilityTier[];
  change_refused: string | null;
  checkpoint_evidence: "required" | "configured" | "not-configured";
  checkpoint_signer_profiles: string[] | null;
  credential_backends: (
    "process" | "keyring" | "mounted-file" | "managed-identity"
  )[];
  credential_references: string[];
  data_destinations: DataDestination[] | null;
  deny_egress_by_default: boolean;
  isolated_worker: "required" | "configured" | "not-configured";
  manifest: string | null;
  manifest_sha256: string | null;
  model_catalog_endpoints: string[];
  model_endpoints: string[];
  platform_id: string;
  policy_id: string;
  source: "deployment" | "platform";
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
 * One research MCP server without credential values or references.
 */
export interface McpServerResponse {
  authentication: McpAuthenticationResponse;
  destination: "allowed" | "refused";
  enabled: boolean;
  enabled_tools: string[];
  label: string;
  origin: string;
  server_id: string;
  service_id: string | null;
  tools: McpSelectedToolResponse[];
  url: string;
}
/**
 * Authentication state the gateway judges from the token it holds.
 */
export interface McpAuthenticationResponse {
  expires_at: string | null;
  status: "missing" | "valid" | "expired" | "unknown-expiry";
}
/**
 * One researcher-selected tool and its reviewed definition digest.
 */
export interface McpSelectedToolResponse {
  annotations: McpToolAnnotationsResponse;
  definition: string;
  description: string;
  name: string;
  parameters: string;
  sha256: string;
  status: "ready" | "changed" | "missing" | "disallowed" | "unsupported";
  worker_tool: string;
}
/**
 * Behavior hints a server advertises for a tool; never authorization.
 */
export interface McpToolAnnotationsResponse {
  destructive: boolean | null;
  idempotent: boolean | null;
  open_world: boolean | null;
  read_only: boolean | null;
}
/**
 * Research MCP servers, destinations and availability.
 */
export interface McpServersResponse {
  availability: "available" | "unavailable";
  credential_store: CredentialStoreAvailabilityResponse;
  destinations: McpDestinationResponse[];
  servers: McpServerResponse[];
  unavailable_code: string | null;
  unavailable_reason: string | null;
}
/**
 * One deployment-approved origin and the tools researchers may select there.
 */
export interface McpDestinationResponse {
  credential_binding: string;
  max_result_bytes: number;
  origin: string;
  private_address: boolean;
  service_id: string;
  tools: string[];
}
/**
 * The result of connecting to one server and listing its tools.
 */
export interface McpServerTestResponse {
  operations: McpOperationsResponse | null;
  prompts: McpCapabilityResponse;
  resources: McpCapabilityResponse;
  server: McpServerResponse;
  tools: McpDiscoveredToolResponse[];
}
/**
 * What a platform operation service declares; discovery authorizes nothing.
 */
export interface McpOperationsResponse {
  capabilities: McpOptionalCapabilityResponse[];
  limitations: string[];
  operations: McpOperationResponse[];
  status: "available" | "unavailable";
}
/**
 * An optional platform capability; one the service does not declare is unavailable.
 */
export interface McpOptionalCapabilityResponse {
  capability: "gpu-resources" | "dataset-attachment" | "application-discovery";
  reason: string | null;
  status: "available" | "not-permitted" | "unavailable";
}
/**
 * One operation a platform service declares, with the permission it requires.
 *
 * ``consistent`` is false when the declared operation differs from the role the
 * deployment gives the tool or from its read-only hint.
 */
export interface McpOperationResponse {
  consistent: boolean;
  listed: boolean;
  operation: "submit" | "status" | "cancel" | "proposal";
  permitted: boolean | null;
  read_only: boolean | null;
  required_permission: string;
  tool: string;
}
/**
 * Whether a server advertises a capability Heartwood does not use.
 */
export interface McpCapabilityResponse {
  advertised: boolean;
  support: "unsupported";
}
/**
 * One tool listed by a test; listing authorizes nothing.
 */
export interface McpDiscoveredToolResponse {
  accepts_operation_binding: boolean;
  allowed: boolean;
  annotations: McpToolAnnotationsResponse;
  changed: boolean;
  definition: string;
  description: string;
  name: string;
  parameters: string;
  selected: boolean;
  sha256: string;
}
/**
 * Bundled and explicitly installed Skills.
 */
export interface SkillSettingsResponse {
  skills: SkillSummaryResponse[];
}
/**
 * One bundled, signed-catalog, installed, or local-candidate Skill.
 */
export interface SkillSummaryResponse {
  approval_summary: string;
  archive_size: number | null;
  compatibility_reason: string | null;
  controlled_data_ready: boolean;
  data_access_summary: string;
  dataset_types: string[];
  declared_tools: string[];
  description: string;
  installable: boolean;
  name: string;
  phi_risk: "none" | "reads-phi" | "writes-outside-boundary";
  requires_network: boolean;
  review: "repository-reviewed" | "local-unreviewed";
  revocation_reason: string | null;
  skill_id: string;
  source: "bundled" | "catalog" | "installed" | "local-candidate";
  source_id: string;
  source_revision: string | null;
  status: "available" | "active" | "revoked" | "unsupported";
  tree_sha256: string;
  unexpected_content: string[];
  version: string;
}
/**
 * Validated research-specialist catalog shared by every interface.
 */
export interface SpecialistSettingsResponse {
  specialists: SpecialistRoleResponse[];
}
/**
 * One bounded research specialist exposed by the shared gateway.
 */
export interface SpecialistRoleResponse {
  availability: "available" | "unavailable";
  capability: "advisory" | "project-actions";
  description: string;
  label: string;
  max_budget_usd: number;
  max_iterations: number;
  model_route: "inherit";
  permission_mode: "always_confirm";
  presentation_summary: string;
  skills: string[];
  specialist_id: string;
  tools: string[];
  unavailable_reason: string | null;
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
 * Project-local scientific records; no immutable-retention claim.
 */
export interface ExperimentCollection {
  retention: "project-local";
  runs: ExperimentRun[];
  schema_version: "heartwood.experiment-collection.v1";
}
/**
 * One shared projection derived from the scientific execution journal.
 */
export interface ExperimentRun {
  attempt: number;
  definition: ExperimentDefinition;
  evidence: ExperimentEvidence[];
  exit_code: number | null;
  outputs: ExperimentFile[];
  run_id: string;
  schema_version: "heartwood.experiment-run.v1";
  started_at: string;
  status: ExperimentStatus;
  updated_at: string;
}
/**
 * Immutable execution inputs; retries cannot silently change an experiment.
 */
export interface ExperimentDefinition {
  actor_ref: Reference;
  /**
   * @maxItems 256
   */
  code: ExperimentFile[];
  /**
   * @maxItems 256
   */
  code_output_paths: ExperimentPath[];
  entry_point: ExperimentPath | null;
  environment: ExperimentEnvironment;
  git_dirty: boolean | null;
  git_revision: string | null;
  /**
   * @maxItems 256
   */
  inputs: ExperimentFile[];
  invocation_sha256: Digest;
  /**
   * @maxItems 256
   */
  output_paths: ExperimentPath[];
  parameters_sha256: Digest;
  source: "python" | "shell" | "heartwood";
  stage: ExperimentStage | null;
}
/**
 * A declared project file observed at a boundary, without its contents.
 */
export interface ExperimentFile {
  path: ExperimentPath;
  sha256: Digest;
  size_bytes: number;
}
/**
 * Fingerprint of an environment description, not an environment attestation.
 */
export interface ExperimentEnvironment {
  kind: "python" | "container" | "declared";
  sha256: Digest;
  source: "observed" | "declared";
}
/**
 * Association with the owning Heartwood session and research stage.
 */
export interface ExperimentStage {
  correction_id: Reference | null;
  session_id: Reference;
  stage_id: Reference;
  tool_call_id: Reference | null;
  workflow_run_id: Reference;
  workflow_sha256: Digest;
}
/**
 * A link to an existing session event, never a copy of its command or result.
 */
export interface ExperimentEvidence {
  event_id: string;
  event_sha256: Digest;
  kind: Reference;
}
/**
 * Plans, estimates, budgets and outcomes, the same for every interface.
 */
export interface PlanningProjection {
  /**
   * The latest observation of each platform budget.
   */
  budgets?: BudgetObservation[];
  launch?: PlatformLaunchContext | null;
  /**
   * Whether this deployment connects a planning service.
   */
  offered: boolean;
  outcomes?: PlanningOutcome[];
  plans?: PlanProjection[];
  schema_version?: "heartwood.planning-projection.v1";
  services?: PlanningServices | null;
}
/**
 * Platform-reported snapshot, not a local ledger or a guarantee of future availability.
 */
export interface BudgetObservation {
  allocated?: string | null;
  available?: string | null;
  budget_id: string;
  constraints?: string[];
  denomination: CostDenomination;
  limitations?: string[];
  observed_at: string;
  platform_record_ref: string;
  project_context_id: string;
  reserved?: string | null;
  schema_version?: "heartwood.budget-observation.v1";
  spent?: string | null;
}
/**
 * Currency or platform allocation unit; never a resource measurement unit.
 */
export interface CostDenomination {
  kind: "currency" | "allocation-unit";
  unit: string;
}
/**
 * A platform-issued context binding, separate from Heartwood's local project.
 *
 * The process working directory remains the local project boundary. The platform's
 * project and billing references do not replace it and cannot be selected by an agent.
 * Parsing this record does not authenticate its issuer or activate a billing binding.
 */
export interface PlatformLaunchContext {
  /**
   * Platform authority that issued this context.
   */
  authority_ref: string;
  /**
   * Opaque platform-owned billing/grant binding; never account details or a secret.
   */
  billing_attribution_ref?: string | null;
  expires_at?: string | null;
  issued_at: string;
  platform_id: string;
  /**
   * Exact applicable platform policy reference.
   */
  policy_ref: string;
  /**
   * Opaque launch-context identity used by estimates and operation requests.
   */
  project_context_id: string;
  /**
   * Platform authorization/resource project identity.
   */
  project_id: string;
  schema_version?: "heartwood.platform-launch-context.v1";
}
/**
 * A planning condition with its stable diagnostic and the record it concerns.
 */
export interface PlanningOutcome {
  code: string;
  diagnostic: string;
  next_action: string;
  /**
   * The service, estimate, budget or operation concerned.
   */
  subject: string;
  title: string;
}
/**
 * All recorded revisions of one plan.
 */
export interface PlanProjection {
  comparison: EstimateComparison[];
  plan_id: string;
  revisions: PlanRevision[];
}
/**
 * One complete-plan estimate for comparing revisions and runtime configurations.
 */
export interface EstimateComparison {
  /**
   * Uses the denomination of the plan's first estimate.
   */
  comparable: boolean;
  estimate_id: string;
  prediction: PlanningReference;
  runtime_profile_refs: string[];
  state: "current" | "expired";
  total: EstimatedCost;
}
/**
 * An immutable revision of a plan, prediction, or other project-local record.
 */
export interface PlanningReference {
  record_id: string;
  revision: number;
}
/**
 * Platform-produced cost range; unknown is not zero and partial is not complete.
 */
export interface EstimatedCost {
  coverage: "complete" | "partial" | "unknown";
  denomination: CostDenomination;
  expected?: string | null;
  limitations?: string[];
  lower?: string | null;
  upper?: string | null;
}
/**
 * One prediction revision with its linked platform records and review states.
 */
export interface PlanRevision {
  elements: ElementReview[];
  estimates: EstimateState[];
  overview: PlanningOverview;
}
/**
 * Review state of one plan element; researcher approval is not platform authorization.
 */
export interface ElementReview {
  /**
   * Prediction revision whose reviewed proposal was approved.
   */
  approved_in?: PlanningReference | null;
  element_id: string;
  kind: "exploration" | "batch" | "inference";
  platform:
    | "authorized"
    | "denied"
    | "pending"
    | "unknown"
    | "revoked"
    | "not-requested";
  review:
    "approved" | "denied" | "unchanged-since-approval" | "requires-review";
  runtime_profile_ref: string;
}
/**
 * Freshness of one platform estimate as of the projection time.
 */
export interface EstimateState {
  /**
   * A budget observed after the estimate differs from the one before it.
   */
  budget_changed: boolean;
  estimate_id: string;
  state: "current" | "expired";
}
/**
 * One project/plan revision's linked view; neither an accounting nor approval engine.
 */
export interface PlanningOverview {
  authorizations?: PlatformAuthorization[];
  budgets?: BudgetObservation[];
  estimates?: PlatformEstimate[];
  operation_sets?: ReviewedOperationSet[];
  operations?: PlatformOperationObservation[];
  prediction: WorkloadPrediction;
  researcher_decisions?: ResearcherDecision[];
  schema_version?: "heartwood.planning-overview.v1";
}
/**
 * Observed platform decision/reservation; a parsed claim grants no actual authority.
 */
export interface PlatformAuthorization {
  authorization_id: string;
  budget_id?: string | null;
  document_sha256: string;
  observed_at: string;
  platform_record_ref: string;
  project_context_id: string;
  proposal: PlanningReference;
  reason: string;
  reservation_ref?: string | null;
  schema_version?: "heartwood.platform-authorization.v1";
  status: "authorized" | "denied" | "pending" | "unknown" | "revoked";
  valid_until?: string | null;
}
/**
 * Completed estimate result, identical for immediate and asynchronous requests.
 */
export interface PlatformEstimate {
  /**
   * @minItems 1
   */
  covered_element_ids: string[];
  created_at: string;
  element_estimates?: ElementEstimate[];
  estimate_id: string;
  exclusions?: string[];
  focus_evidence?: FocusEvidenceReference[];
  plan: PlanningReference;
  prediction: PlanningReference;
  pricing_assumptions?: string[];
  project_context_id: string;
  schema_version?: "heartwood.platform-estimate.v1";
  scope: "plan" | "elements";
  total: EstimatedCost;
  valid_until: string;
}
/**
 * Element cost coverage; included components are breakdowns, not additional totals.
 */
export interface ElementEstimate {
  cost: EstimatedCost;
  /**
   * @minItems 1
   */
  element_ids: string[];
  included_in_total: boolean;
}
/**
 * Unmodified FOCUS pricing/cost evidence, retaining its version and draft revision.
 */
export interface FocusEvidenceReference {
  content_sha256?: string | null;
  dataset: string;
  draft_revision?: string | null;
  reference: string;
  specification_version: string;
}
/**
 * Parsed proposal plus the hash/reference of its exact immutable JSON bytes.
 *
 * Parsing this envelope does not verify referenced bytes. Use reviewed_operation_set
 * with the actual reviewed document to establish its content binding.
 */
export interface ReviewedOperationSet {
  document_ref: string;
  document_sha256: string;
  proposal: OperationSetProposal;
}
/**
 * The exact JSON document reviewed by a researcher, not a reusable spending envelope.
 */
export interface OperationSetProposal {
  created_at: string;
  estimates?: PlatformEstimate[];
  /**
   * @minItems 1
   */
  operations: ProposedOperation[];
  plan: PlanningReference;
  prediction: PlanningReference;
  project_context_id: string;
  proposal: PlanningReference;
  schema_version?: "heartwood.operation-set-proposal.v1";
}
/**
 * One exact operation with its review-visible effects and native request binding.
 */
export interface ProposedOperation {
  data_scope: string;
  description: string;
  destination: string;
  /**
   * @minItems 1
   */
  element_ids: string[];
  estimate_ids?: string[];
  operation_id: string;
  request: BackendRequestReference;
  resource_limits?: OperationResourceLimit[];
}
/**
 * Exact native request bytes; this record does not clone backend request schemas.
 */
export interface BackendRequestReference {
  protocol: string;
  request_ref: string;
  request_sha256: string;
  service_id: string;
}
/**
 * A declared hard operation limit; enforcement belongs to the platform.
 */
export interface OperationResourceLimit {
  /**
   * Finite nonnegative decimal string; no exponent, rounding, or binary float.
   */
  maximum: string;
  metric: string;
  unit: string;
}
/**
 * Backend execution observation, separate from MCP task/request status and cancellation.
 */
export interface PlatformOperationObservation {
  backend_operation_ref: string;
  limitations?: string[];
  observed_at: string;
  operation_id: string;
  platform_record_ref: string;
  project_context_id: string;
  schema_version?: "heartwood.platform-operation-observation.v1";
  service_id: string;
  status:
    | "submitted"
    | "queued"
    | "running"
    | "completed"
    | "failed"
    | "cancelled"
    | "denied"
    | "unknown";
}
/**
 * Agent/researcher workload prediction, not a platform price or execution request.
 */
export interface WorkloadPrediction {
  assumptions?: string[];
  created_at: string;
  /**
   * @minItems 1
   */
  elements: WorkloadElement[];
  evidence?: WorkloadEvidence[];
  plan: PlanningReference;
  prediction: PlanningReference;
  project_context_id: string;
  schema_version?: "heartwood.workload-prediction.v1";
  supersedes?: PlanningReference | null;
  uncertainty: string;
}
/**
 * One separately estimable exploration, batch, or inference element.
 */
export interface WorkloadElement {
  assumptions?: string[];
  description: string;
  element_id: string;
  evidence_ids?: string[];
  kind: "exploration" | "batch" | "inference";
  quantities: WorkloadQuantity[];
  runtime_profile_ref: string;
}
/**
 * A predicted quantity and uncertainty; units identify the actual measurement scale.
 */
export interface WorkloadQuantity {
  expected?: string | null;
  lower?: string | null;
  metric: string;
  uncertainty: string;
  unit: string;
  upper?: string | null;
}
/**
 * Project-local evidence or researcher correction; absent measurements remain unknown.
 */
export interface WorkloadEvidence {
  content_sha256?: string | null;
  description: string;
  evidence_id: string;
  kind: "prior-run" | "researcher-input" | "sample-run" | "correction";
  observed_at: string;
  project_context_id: string;
  reference: string;
}
/**
 * Session-audited decision over exact proposal bytes; not platform authorization.
 */
export interface ResearcherDecision {
  actor_ref: string;
  audit_ref: string;
  decided_at: string;
  decision: "approved" | "denied" | "withdrawn";
  decision_id: string;
  document_sha256: string;
  project_context_id: string;
  proposal: PlanningReference;
  schema_version?: "heartwood.researcher-decision.v1";
}
/**
 * What the platform's planning service offered when Heartwood last asked.
 */
export interface PlanningServices {
  budgets: PlanningServiceStatus;
  checked_at?: string | null;
  estimates: PlanningServiceStatus;
}
/**
 * Ask the platform to estimate a recorded prediction, whole or for selected elements.
 */
export interface PlanningEstimateRequest {
  /**
   * Elements to estimate; required for, and only for, elements scope.
   */
  element_ids?: string[];
  prediction: PlanningReference;
  scope?: "plan" | "elements";
}
/**
 * Interpreter identity and declared installed versions without paths or secrets.
 */
export interface PythonEnvironmentSnapshot {
  implementation: MetadataText;
  machine: MetadataText;
  /**
   * @maxItems 10000
   */
  packages: any[][];
  python: MetadataText;
  schema_version?: "heartwood.python-environment.v1";
  system: MetadataText;
}
/**
 * Canonical record bytes and their digest, not a signed checkpoint.
 */
export interface ExperimentExport {
  jsonl: string;
  schema_version: "heartwood.experiment-export.v1";
  sha256: Digest;
}
/**
 * Read-only workflow discovery shared by every interface.
 */
export interface WorkflowCatalog {
  workflows: WorkflowCatalogEntry[];
}
/**
 * One maintained workflow and any checks missing from this runtime.
 */
export interface WorkflowCatalogEntry {
  /**
   * Expose runtime support, not model qualification or execution permission.
   */
  available: boolean;
  definition: WorkflowDefinition;
  unavailable_checks: WorkflowIdentifier[];
}
/**
 * Pinned sequential task contract; advisory concurrency does not change its order.
 */
export interface WorkflowDefinition {
  /**
   * @minItems 1
   * @maxItems 64
   */
  artifacts: WorkflowArtifact[];
  budget: ExecutionBudget;
  description: WorkflowText;
  /**
   * @minItems 1
   * @maxItems 32
   */
  inputs: WorkflowInput[];
  label: WorkflowText;
  schema_version: "heartwood.workflow-definition.v1";
  /**
   * @minItems 1
   * @maxItems 32
   */
  stages: WorkflowStage[];
  version: number;
  workflow_id: WorkflowIdentifier;
}
/**
 * A declared output beneath the workflow's project-relative output directory.
 */
export interface WorkflowArtifact {
  artifact_id: WorkflowIdentifier;
  label: WorkflowText;
  media_type:
    "text/markdown" | "text/csv" | "text/x-python" | "application/json";
  relative_path: string;
}
/**
 * Observed admission limits, not a provider-side spending or preemption cap.
 */
export interface ExecutionBudget {
  maximum_actions: number;
  maximum_model_calls: number;
  maximum_reported_cost_usd: number;
  maximum_seconds: number;
  maximum_tokens: number;
}
/**
 * An explicit researcher-supplied file or research objective.
 */
export interface WorkflowInput {
  description: WorkflowText;
  input_id: WorkflowIdentifier;
  kind: "file" | "text";
  label: WorkflowText;
}
/**
 * One ordered task with explicit context, outputs, and completion gates.
 */
export interface WorkflowStage {
  budget: ExecutionBudget;
  /**
   * @minItems 1
   */
  checks: WorkflowCheck[];
  instruction: WorkflowText;
  label: WorkflowText;
  /**
   * @minItems 1
   */
  reads: WorkflowIdentifier[];
  reviewer_gate: "none" | "researcher";
  skill_ids: WorkflowIdentifier[];
  specialist_ids: WorkflowIdentifier[];
  stage_id: WorkflowIdentifier;
  /**
   * @minItems 1
   */
  writes: WorkflowIdentifier[];
}
/**
 * A required deterministic check resolved by the gateway's evaluator registry.
 */
export interface WorkflowCheck {
  /**
   * @minItems 1
   */
  artifact_ids: WorkflowIdentifier[];
  check_id: WorkflowIdentifier;
  description: WorkflowText;
  evaluator_id: WorkflowIdentifier;
}
/**
 * Explicitly bind a new workflow to an unused session.
 */
export interface WorkflowStart {
  action: "start";
  inputs: {
    [k: string]: WorkflowInputValue;
  };
  output_directory: string;
  workflow_id: WorkflowIdentifier;
}
/**
 * Apply a transition only to the exact run and revision the researcher saw.
 */
export interface WorkflowTransition {
  action: "run" | "evaluate" | "cancel" | "prepare-parallel-review";
  revision: number;
  run_id: string;
}
/**
 * Request advisory review; parallel work requires the exact journaled preview.
 */
export interface WorkflowReviewRequest {
  action: "request-review";
  parallel_review_fingerprint: string | null;
  revision: number;
  run_id: string;
}
/**
 * Accept or reject checked stage evidence, never the underlying tool actions.
 */
export interface WorkflowReview {
  action: "review";
  approved: boolean;
  evidence_fingerprint: string;
  revision: number;
  run_id: string;
}
/**
 * Authorize bounded parent-agent corrections without approving their tool actions.
 */
export interface WorkflowCorrectionRequest {
  action: "correct";
  maximum_attempts: number;
  revision: number;
  run_id: string;
}
/**
 * Select the action-review mode.
 */
export interface ActionReviewRequest {
  mode: ActionReviewMode;
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
 * Add one research MCP server at a deployment-approved origin.
 */
export interface McpServerAddRequest {
  label: string;
  remember?: boolean;
  server_id: string;
  token?: string | null;
  url: string;
}
/**
 * Enable or disable one research MCP server for new sessions.
 */
export interface McpServerEnabledRequest {
  enabled: boolean;
}
/**
 * Connect to one research MCP server and list its tools.
 */
export interface McpServerTestRequest {
  remember?: boolean;
  token?: string | null;
}
/**
 * Select reviewed tool definitions of one research MCP server.
 */
export interface McpServerToolsRequest {
  remember?: boolean;
  token?: string | null;
  /**
   * @maxItems 64
   */
  tools: McpToolChoice[];
}
/**
 * One tool and the digest of the definition the researcher reviewed.
 */
export interface McpToolChoice {
  name: string;
  sha256: string;
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
 * Export the selected Heartwood-managed model to one new bundle path.
 */
export interface ModelTransferExportRequest {
  path: string;
}
/**
 * Import one inspected bundle after explicit license review.
 */
export interface ModelTransferImportRequest {
  approved: boolean;
  manifest_sha256: string;
  path: string;
}
/**
 * Inspect one portable Heartwood model bundle without importing it.
 */
export interface ModelTransferInspectRequest {
  path: string;
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
 * Inspect one Skill from a deployment-approved signed source.
 */
export interface SkillInspectRequest {
  name: string;
  source_id?: string | null;
}
/**
 * Install one explicitly approved signed Skill revision.
 */
export interface SkillInstallRequest {
  approved: boolean;
  expected_tree_sha256: string;
  name: string;
  source_id?: string | null;
}
/**
 * Inspect one advanced local Agent Skill source.
 */
export interface SkillLocalInspectRequest {
  source: string;
}
/**
 * Install one explicitly approved local, unreviewed Skill revision.
 */
export interface SkillLocalInstallRequest {
  approved: boolean;
  expected_tree_sha256: string;
  source: string;
}
/**
 * Refresh all configured signed sources or one named source.
 */
export interface SkillRefreshRequest {
  source_id?: string | null;
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
