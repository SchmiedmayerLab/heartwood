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
  | "detect"
  | "approve"
  | "deny"
  | "chat"
  | "run"
  | "pause"
  | "resume"
  | "replay"
  | "audit.export";

export type EventKind =
  | "command.received"
  | "detection.proposed"
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
}

export interface ApprovalControl {
  targetType: string;
  targetId: string;
  label: string;
  decision: string | null;
}

export interface SessionViewModel {
  sessionId: string;
  eventCount: number;
  activity: ActivityItem[];
  conversation: ConversationMessage[];
  approvalControls: ApprovalControl[];
  paused: boolean;
}

export type CredentialKind =
  "environment" | "file" | "managed-identity" | "none";
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
  description: string | null;
  credential_status?: string;
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

export interface ModelSettings {
  schema_version: "heartwood.model-settings.v1";
  active_profile: string | null;
  profiles: ModelProfile[];
  presets: ModelPreset[];
}

export interface ModelValidation {
  profile: ModelProfile;
  credential_status: string;
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
  minimum_resource_envelope: string | null;
  recommended_resource_envelope: string | null;
}

export interface ModelDownload {
  artifact_id: string;
  status: "downloading" | "error" | "ready";
  path: string | null;
  error: string | null;
}

export interface ModelArtifacts {
  schema_version: "heartwood.local-model-catalog.v1";
  artifacts: ModelArtifact[];
  downloads: ModelDownload[];
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
