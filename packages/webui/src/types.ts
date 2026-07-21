/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type CommandKind =
  "approve" | "deny" | "chat" | "pause" | "resume" | "replay" | "audit.export";

export type EventKind =
  | "command.received"
  | "approval.recorded"
  | "policy.decision.recorded"
  | "model_call.decision.recorded"
  | "user_message.recorded"
  | "agent_message.emitted"
  | "tool_call.proposed"
  | "confirmation.requested"
  | "confirmation.resolved"
  | "tool.execution.recorded"
  | "session.paused"
  | "session.resumed"
  | "audit.export.recorded"
  | "error.recorded";

export interface SessionCommand {
  schema_version: "heartwood.session-command.v1";
  command_id: string;
  session_id: string;
  kind: CommandKind;
  actor_id: string;
  created_at: string;
  payload: Record<string, JsonValue>;
}

export interface SessionEvent {
  schema_version: "heartwood.session-event.v1";
  event_id: string;
  session_id: string;
  sequence: number;
  kind: EventKind;
  occurred_at: string;
  payload: Record<string, JsonValue>;
  previous_event_hash: string | null;
}

export type SessionStatus = "empty" | "idle" | "waiting" | "paused" | "error";

export interface SessionSummary {
  session_id: string;
  title: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  event_count: number;
}

export interface SessionList {
  sessions: SessionSummary[];
}

export interface AuditExport {
  filename: string;
  content: string;
}

export interface ActivityItem {
  sequence: number;
  kind: EventKind;
  label: string;
  detail: string;
}

export interface ConversationMessage {
  id: string;
  sequence: number;
  role: "user" | "agent" | "trace";
  label: string;
  content: string;
  detail: string | null;
  technicalDetail?: string | null;
}

export interface ApprovalControl {
  targetType: string;
  targetId: string;
  label: string;
  toolName: string;
  risk: string | null;
  summary: string | null;
  arguments: Record<string, JsonValue>;
  decision: string | null;
}

export interface SessionContext {
  modelEndpoint: string | null;
  modelDecision: string | null;
  modelReason: string | null;
}

export interface SessionViewModel {
  sessionId: string;
  eventCount: number;
  activity: ActivityItem[];
  conversation: ConversationMessage[];
  approvalControls: ApprovalControl[];
  context: SessionContext;
  paused: boolean;
}

export type CredentialKind =
  "environment" | "file" | "managed-identity" | "none";
export type CredentialStatus = "available" | "configured" | "missing";
export type ActionConfirmationMode = "always-confirm" | "confirm-risky";

export interface ActionModeOption {
  mode: ActionConfirmationMode;
  label: string;
  allowed: boolean;
}

export interface ActionSettings {
  schema_version: "heartwood.action-settings.v1";
  confirmation_mode: ActionConfirmationMode;
  modes: ActionModeOption[];
}

export type ReadinessState =
  "ready" | "setup-required" | "compute-required" | "recovery-required";

export interface ReadinessCheck {
  check_id: string;
  status: "pass" | "warning" | "fail";
  summary: string;
  code?: string;
  title?: string;
  next_action?: string;
  documentation_path?: string;
}

export interface ProjectReadiness {
  state: ReadinessState;
  platform_id: string;
  project_root: string;
  state_root: string;
  evidence: string[];
  checks: ReadinessCheck[];
}

export type InterfaceKind = "terminal" | "web" | "notebook";

export interface PlatformCapabilities {
  platform_id: string;
  display_name: string;
  interfaces: InterfaceKind[];
  browser_route: "direct" | "jupyter-proxy" | "unavailable";
  managed_runtimes: Array<"llama-cpp" | "vllm">;
  scheduler: "none" | "provisioned" | "slurm";
  persistent_storage: string;
  credential_backends: Array<
    "process" | "keyring" | "mounted-file" | "managed-identity"
  >;
  model_sources: Array<
    "anthropic" | "custom" | "heartwood" | "openai" | "stanford-ai-api-gateway"
  >;
  managed_model_connections: string[];
  validation_level: "ci" | "ci-and-live-synthetic";
}

export type SetupPhase =
  | "project-review"
  | "connection-required"
  | "credential-required"
  | "model-required"
  | "compute-required"
  | "ready"
  | "recovery-required";

export interface StartupPlan {
  phase: SetupPhase;
  interface: InterfaceKind;
  platform_id: string;
  project_root: string;
  state_root: string;
  summary: string;
  next_action: string;
  access_url: string | null;
  requires_compute: boolean;
  requires_confirmation: boolean;
  interface_supported: boolean;
  readiness: ProjectReadiness;
  capabilities: PlatformCapabilities;
}

export interface ModelProfile {
  profile_id: string;
  model: string;
  policy_endpoint: string;
  capability_tier: "autonomous" | "experimental" | "supervised";
  base_url: string | null;
  credential_kind: CredentialKind;
  api_key_env: string | null;
  api_key_file: string | null;
  api_version: string | null;
  aws_region_name: string | null;
  aws_profile_name: string | null;
  max_input_tokens?: number | null;
  max_output_tokens?: number | null;
  description: string | null;
  credential_status?: CredentialStatus;
}

export type ModelConnectionProtocol =
  "anthropic" | "openai" | "openai-compatible" | "static";

export type ModelConnectionSource = "built-in" | "platform" | "user";
export type ModelConnectionGroup =
  | "compatible-service"
  | "heartwood-managed"
  | "hosted-provider"
  | "research-environment";

export interface ModelConnection {
  connection_id: string;
  label: string;
  protocol: ModelConnectionProtocol;
  model_prefix: string;
  source: ModelConnectionSource;
  credential_kind: CredentialKind;
  policy_endpoint: string | null;
  catalog_endpoint: string | null;
  base_url: string | null;
  api_key_env: string | null;
  api_key_file: string | null;
  api_version: string | null;
  aws_region_name: string | null;
  aws_profile_name: string | null;
  description: string;
  static_models: string[];
  group: ModelConnectionGroup;
  group_label: string;
  accepts_token: boolean;
  credential_status: CredentialStatus;
}

export interface ModelCatalogEntry {
  model_id: string;
  display_name: string;
  execution_model: string;
  availability: "available" | "experimental" | "unsupported";
  reason: string;
  context_window: number | null;
  supports_tools: boolean | null;
}

export interface ModelCatalog {
  schema_version: "heartwood.model-catalog.v1";
  connection: ModelConnection;
  models: ModelCatalogEntry[];
  refreshed_at: number;
}

export interface ModelCatalogRequest {
  connection_id: string;
  token?: string;
  base_url?: string;
  refresh?: boolean;
  remember?: boolean;
}

export interface ModelConnectRequest {
  connection_id: string;
  model_id: string;
  token?: string;
  base_url?: string;
  manual?: boolean;
  remember?: boolean;
}

export interface CredentialStoreAvailability {
  backends: Array<"process" | "keyring">;
  default_backend: "process" | "keyring";
  persistence_available: boolean;
  persistence_description: string;
}

export interface CredentialBindingStatus {
  binding_id: string;
  configured: boolean;
  source: "environment" | "keyring" | "process" | "unavailable" | null;
  error?: string | null;
}

export interface CredentialSettings {
  store: CredentialStoreAvailability;
  bindings: CredentialBindingStatus[];
}

export interface ModelPreset {
  preset_id: string;
  label: string;
  model_prefix: string;
  credential_kind: CredentialKind;
  api_key_env: string | null;
  base_url: string | null;
  policy_endpoint: string | null;
  description: string;
}

export type ModelSource =
  "anthropic" | "custom" | "heartwood" | "openai" | "stanford-ai-api-gateway";

export interface ModelSourceOption {
  source_id: ModelSource;
  connection_id: string;
  label: string;
  description: string;
  selected: boolean;
}

export interface ModelSettings {
  schema_version: "heartwood.model-settings.v1";
  active_profile: string | null;
  model_source: string | null;
  profiles: ModelProfile[];
  connections: ModelConnection[];
  presets: ModelPreset[];
  source_options: ModelSourceOption[];
  credential_store: CredentialStoreAvailability;
  credential_bindings: CredentialBindingStatus[];
}

export interface ModelValidation {
  profile: ModelProfile;
  credential_status: CredentialStatus;
  action_confirmation_mode: ActionConfirmationMode;
  policy_decision: {
    decision: string;
    endpoint: string;
    reason: string;
  };
}

export interface ModelArtifact {
  artifact_id: string;
  runtime_profile: string;
  purpose: string;
  source_repository: string;
  source_path: string;
  source_revision: string;
  artifact_format: string;
  artifact_size_bytes: number;
  artifact_sha256: string;
  license_posture: string;
  model_alias: string;
  context_window: number;
  minimum_resource_envelope: string | null;
  recommended_resource_envelope: string | null;
  recommended: boolean;
}

export interface ModelDownload {
  model_id: string;
  status: "downloading" | "error" | "ready";
  bytes_downloaded: number;
  bytes_total: number;
  path: string | null;
  error: string | null;
}

export type LocalModelRuntime = "llama-cpp" | "vllm";
export type LocalModelTier = "standard" | "powerful" | "maximum";
export type LocalModelQualification = "candidate" | "qualified";
export type ToolCallParser = "hermes" | "openai" | "qwen3_coder";

export interface LocalModelChoice {
  model_id: string;
  label: string;
  purpose: string;
  runtime: LocalModelRuntime;
  source_repository: string;
  source_revision: string;
  source_path: string | null;
  size_bytes: number;
  minimum_free_bytes: number;
  license_id: string;
  license_posture: string;
  catalog_source: "catalog" | "user-selected";
  context_window: number;
  maximum_context_window: number;
  precision: string;
  tier: LocalModelTier;
  qualification: LocalModelQualification;
  minimum_gpu_count: number;
  minimum_gpu_memory_bytes: number;
  recommended_ram_bytes: number;
  recommended_disk_bytes: number;
  tool_call_parser: ToolCallParser | null;
  tensor_parallel_size: number;
  startup_seconds_min: number;
  startup_seconds_max: number;
  download_policy: string | null;
  allow_patterns: string[];
  ignore_patterns: string[];
  validated_platforms: string[];
  qualification_test: string | null;
  artifact_sha256: string | null;
  minimum_resource_envelope: string | null;
  recommended_resource_envelope: string | null;
  active: boolean;
  available: boolean;
  selected: boolean;
  availability_reason: string;
  recommended: boolean;
}

export interface GpuCapacity {
  label: string;
  gpu_model: string;
  gpu_count: number;
  gpu_memory_bytes: number;
  allocation_required: boolean;
  partition: string | null;
}

export interface ModelRepositoryPlan {
  model: LocalModelChoice;
  selection_reason: string;
}

export interface ModelRepositoryRequest {
  repository: string;
  revision?: string;
}

export interface CustomLocalModelDownloadRequest {
  repository: string;
  revision?: string;
}

export interface LocalModelImportRequest {
  path: string;
  repository: string;
  revision: string;
  license: string;
  context_window?: number;
}

export interface LocalModelImportResult {
  model: LocalModelChoice;
  path: string;
  status: "ready";
}

export interface ModelSnapshot {
  snapshot_id: string;
  runtime_profile: string;
  purpose: string;
  source_repository: string;
  source_revision: string;
  expected_size_bytes: number;
  minimum_free_bytes: number;
  license_id: string;
  license_posture: string;
  model_alias: string;
  precision: string;
  tier: LocalModelTier;
  qualification: LocalModelQualification;
  minimum_gpu_count: number;
  minimum_gpu_memory_bytes: number;
  recommended_ram_bytes: number;
  recommended_disk_bytes: number;
  context_window: number;
  maximum_context_window: number;
  tool_call_parser: ToolCallParser;
  tensor_parallel_size: number;
  startup_seconds_min: number;
  startup_seconds_max: number;
  download_policy: string;
  allow_patterns: string[];
  ignore_patterns: string[];
  validated_platforms: string[];
  qualification_test: string | null;
  minimum_resource_envelope: string | null;
  recommended_resource_envelope: string | null;
  recommended: boolean;
}

export interface ModelArtifacts {
  schema_version: "heartwood.local-model-catalog.v1";
  snapshot_schema_version: "heartwood.model-snapshot-catalog.v2";
  artifacts: ModelArtifact[];
  snapshots: ModelSnapshot[];
  models: LocalModelChoice[];
  downloads: ModelDownload[];
  gpu_environment: {
    platform_id: string;
    capacities: GpuCapacity[];
  };
}

export interface SkillSummary {
  name: string;
  skill_id: string;
  description: string;
  trust_tier: string;
  source: "bundled" | "candidate" | "installed";
  approval_summary: string;
  declared_tools: string[];
  requires_network: boolean;
}

export interface SkillSettings {
  skills: SkillSummary[];
}
