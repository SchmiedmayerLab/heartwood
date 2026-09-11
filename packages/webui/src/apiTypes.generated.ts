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
  | ExperimentExport
  | WorkflowCatalog
  | WorkflowRequest
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
  | SkillSettingsResponse
  | SkillSummaryResponse
  | SpecialistSettingsResponse
  | StartupPlanResponse
  | SubscriptionDeviceLoginResponse
  | WorkspaceChangesResponse
  | WorkspaceDiffResponse
  | WorkspaceFileResponse
  | WorkspaceTreeResponse;
export type ActionConfirmationMode = "always-confirm" | "confirm-risky";
export type ActionRisk = "high" | "low" | "medium" | "unknown";
export type LocalModelQualification = "unvalidated" | "qualified";
export type ReasoningParser = "muse_glimmer";
export type LocalModelRuntime = "llama-cpp" | "vllm";
export type LocalModelTier = "standard" | "powerful" | "maximum";
export type ToolCallParser =
  "hermes" | "muse_glimmer" | "openai" | "qwen3_coder";
export type CredentialKind =
  "environment" | "file" | "managed-identity" | "none";
export type CredentialStatus = "available" | "configured" | "missing";
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
export type WorkflowIdentifier = string;
export type WorkflowText = string;
export type WorkflowRequest =
  WorkflowStart | WorkflowTransition | WorkflowReview;
export type WorkflowInputValue = string;

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
  credential_isolation: CredentialIsolationResponse;
  credential_store: CredentialStoreAvailabilityResponse;
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
  unattended_actions_allowed: boolean;
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
  action_confirmation_mode: ActionConfirmationMode;
  credential_isolation: CredentialIsolationResponse;
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
  default_ingress_mode: "direct-loopback" | "jupyter-proxy" | "trusted-proxy";
  display_name: string;
  ingress_modes: ("direct-loopback" | "jupyter-proxy" | "trusted-proxy")[];
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
  action: "run" | "evaluate" | "cancel" | "request-review";
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
